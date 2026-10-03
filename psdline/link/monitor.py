"""链路质量监控。

``Session`` 是一列车的链路状态机，只负责「折叠采样 → 产出判定」，不写流水；
``LinkMonitor`` 在外面加上每列车一把锁、写前日志（先记采样再改内存状态）、
欠拍合成与重启恢复。

关键不变式：**每一个判定都是落盘采样流的纯函数**。实时路径、``restore()``
与 :mod:`psdline.link.replay` 走同一份折叠逻辑，因此事后可以原样重放当时
凭什么这么判。

时序约定：

- 采样与判定都以服务端时钟为准；车端 ``sent_at`` 只做偏移诊断；
- 车端序号（``seq``）单调递增才算新报文：重复序号幂等丢弃（``duplicate``），
  小号乱序记为 ``late``，两者都只留证据、不计入质量、不触发位置更新；
- 判定只挂在采样时刻上（含合成的欠拍），不挂在「查询发生的时刻」上，
  这样重放不需要知道当时谁在什么时候查过状态。
"""

from __future__ import annotations

import math
import threading
from collections import deque

from ..store import JsonlJournal
from .quality import (
    DEGRADED,
    GOOD,
    LOST,
    LinkTuning,
    backoff_interval,
    classify,
    expected_samples,
    rank,
    window_quality,
)
from .sample import LinkSample
from .trust import trust_of


class Session:
    """一列车的链路会话：折叠采样流，产出状态判定。"""

    def __init__(self, train_id: str, tuning: LinkTuning) -> None:
        self.train_id = train_id
        self.tuning = tuning
        self.state = GOOD
        self.started_at: float | None = None
        self.last_sample_at: float | None = None
        self.last_ok_at: float | None = None
        self.consecutive_ok = 0
        self.backoff_level = 0
        self.recovery_started_at: float | None = None
        self.state_since: float | None = None
        self.last_device_seq: int | None = None
        self.local_seq = 0
        self.ok_window: deque[float] = deque()
        self.samples_ok = 0
        self.samples_missed = 0
        self.samples_ignored = 0

    # ---- 折叠 ---------------------------------------------------------

    def ingest(
        self,
        *,
        ok: bool,
        at: float,
        origin: str = "telegram",
        seq: int | None = None,
        rtt_ms: float | None = None,
        sent_at: float | None = None,
        note: str = "",
        count: int = 1,
    ) -> tuple[LinkSample, list[dict]]:
        """实时入口：登记一条采样，返回（采样记录, 判定列表）。"""

        counted = True
        device_seq = seq
        if seq is not None:
            if self.last_device_seq is not None and seq <= self.last_device_seq:
                note = note or ("duplicate" if seq == self.last_device_seq else "late")
                counted = False
            else:
                self.last_device_seq = seq
        skew_s: float | None = None
        if sent_at is not None:
            skew_s = round(at - sent_at, 6)
            if sent_at - at > self.tuning.max_clock_skew_s:
                note = note or "clock_skew"
        self.local_seq += 1
        sample = LinkSample(
            train_id=self.train_id,
            seq=device_seq if device_seq is not None else self.local_seq,
            ok=ok,
            at=at,
            origin=origin,
            counted=counted,
            note=note,
            count=count,
            device_seq=device_seq,
            rtt_ms=rtt_ms,
            sent_at=sent_at,
            skew_s=skew_s,
        )
        decisions = self._apply(sample)
        return sample, decisions

    def fold_record(self, record: dict) -> list[dict]:
        """恢复／重放入口：按落盘的采样记录原样折叠，返回推导出的判定。"""

        sample = LinkSample.from_dict(record)
        if sample.device_seq is not None and sample.counted:
            self.last_device_seq = sample.device_seq
        self.local_seq = max(self.local_seq, sample.seq)
        return self._apply(sample)

    def _apply(self, sample: LinkSample) -> list[dict]:
        """把一条采样折进状态机，返回本次产生的判定（可为空）。"""

        if not sample.counted:
            self.samples_ignored += 1
            return []
        if self.started_at is None:
            self.started_at = sample.at
            self.state_since = sample.at
        if sample.ok:
            self.ok_window.append(sample.at)
            self.last_ok_at = sample.at
            self.consecutive_ok += 1
            self.samples_ok += 1
            if self.state == LOST and self.recovery_started_at is None:
                self.recovery_started_at = sample.at
        else:
            self.consecutive_ok = 0
            self.samples_missed += sample.count
        if self.last_sample_at is None or sample.at > self.last_sample_at:
            self.last_sample_at = sample.at
        return self._transitions(sample.at)

    # ---- 判定 ---------------------------------------------------------

    def cover_s(self, at: float) -> float:
        """会话被采样覆盖的时长。"""

        if self.started_at is None:
            return 0.0
        return max(0.0, at - self.started_at)

    def quality(self, at: float) -> float:
        """``at`` 时刻的窗口质量分。"""

        if self.started_at is None:
            return 1.0
        horizon = at - self.tuning.window_s
        while self.ok_window and self.ok_window[0] < horizon:
            self.ok_window.popleft()
        return window_quality(len(self.ok_window), self.cover_s(at), self.tuning)

    def silence_s(self, at: float) -> float:
        """``at`` 时刻距最后一个好采样的静默时长。"""

        if self.last_ok_at is None:
            return self.cover_s(at)
        return max(0.0, at - self.last_ok_at)

    def _decision(
        self, kind: str, at: float, source: str, target: str, note: str
    ) -> dict:
        return {
            "type": "decision",
            "kind": kind,
            "train_id": self.train_id,
            "at": at,
            "from": source,
            "to": target,
            "quality": round(self.quality(at), 4),
            "silence_s": round(self.silence_s(at), 3),
            "consecutive_ok": self.consecutive_ok,
            "backoff_level": self.backoff_level,
            "window_ok": len(self.ok_window),
            "expected": expected_samples(self.cover_s(at), self.tuning),
            "note": note,
        }

    def _transitions(self, at: float) -> list[dict]:
        """在采样时刻 ``at`` 评估状态迁移：向下立即，向上带迟滞。"""

        tuning = self.tuning
        decisions: list[dict] = []
        target = classify(self.quality(at), self.silence_s(at), tuning)
        if rank(target) > rank(self.state):
            if target == LOST:
                self.backoff_level += 1
                self.recovery_started_at = None
                self.consecutive_ok = 0
            decisions.append(self._decision("state", at, self.state, target, "degraded_link"))
            self.state = target
            self.state_since = at
        elif self.state == LOST and target != LOST:
            age = (
                None
                if self.recovery_started_at is None
                else at - self.recovery_started_at
            )
            if (
                self.consecutive_ok >= tuning.recover_samples
                and age is not None
                and age >= backoff_interval(self.backoff_level, tuning)
            ):
                decisions.append(self._decision("state", at, LOST, DEGRADED, "recovery_observed"))
                self.state = DEGRADED
                self.state_since = at
        elif self.state == DEGRADED and target == GOOD:
            if self.consecutive_ok >= tuning.recover_samples:
                decisions.append(self._decision("state", at, DEGRADED, GOOD, "recovered"))
                self.state = GOOD
                self.state_since = at
        if (
            self.state == GOOD
            and self.backoff_level > 0
            and self.state_since is not None
            and at - self.state_since >= tuning.window_s
        ):
            decisions.append(self._decision("backoff", at, GOOD, GOOD, "backoff_reset"))
            self.backoff_level = 0
        return decisions

    # ---- 快照 ---------------------------------------------------------

    def snapshot(self, at: float) -> dict:
        """返回会话状态快照（只读，不触发判定）。"""

        return {
            "train_id": self.train_id,
            "state": self.state,
            "trust": trust_of(self.state),
            "quality": round(self.quality(at), 4),
            "silence_s": round(self.silence_s(at), 3),
            "consecutive_ok": self.consecutive_ok,
            "backoff_level": self.backoff_level,
            "backoff_interval_s": backoff_interval(self.backoff_level, self.tuning),
            "window_ok": len(self.ok_window),
            "expected": expected_samples(self.cover_s(at), self.tuning),
            "samples_ok": self.samples_ok,
            "samples_missed": self.samples_missed,
            "samples_ignored": self.samples_ignored,
            "last_ok_at": self.last_ok_at,
            "last_sample_at": self.last_sample_at,
        }


class LinkMonitor:
    """列车链路质量监控器：每列车一把锁，采样写前日志，可重启恢复。"""

    def __init__(self, journal: JsonlJournal, tuning: LinkTuning | None = None) -> None:
        self._journal = journal
        self._tuning = tuning or LinkTuning()
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.RLock()

    @property
    def journal(self) -> JsonlJournal:
        return self._journal

    @property
    def tuning(self) -> LinkTuning:
        return self._tuning

    def _lock_for(self, train_id: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(train_id, threading.RLock())

    def _session_for(self, train_id: str) -> Session:
        """取会话（调用方必须已持有该列车的锁）。"""

        session = self._sessions.get(train_id)
        if session is None:
            session = Session(train_id, self._tuning)
            self._sessions[train_id] = session
        return session

    # ---- 写入 ---------------------------------------------------------

    def ingest(
        self,
        train_id: str,
        *,
        ok: bool = True,
        seq: int | None = None,
        rtt_ms: float | None = None,
        sent_at: float | None = None,
        now: float,
    ) -> dict:
        """登记一条车地报文采样：先写流水再改内存，返回当前判定快照。"""

        if not train_id:
            return {
                "counted": False,
                "note": "missing_train",
                "trust": None,
                "state": None,
                "quality": None,
                "backoff_level": 0,
                "decisions": [],
            }
        with self._lock_for(train_id):
            session = self._session_for(train_id)
            sample, decisions = session.ingest(
                ok=ok, at=now, origin="telegram", seq=seq, rtt_ms=rtt_ms, sent_at=sent_at
            )
            self._journal.append({"type": "sample", **sample.as_dict()})
            for decision in decisions:
                self._journal.append(decision)
            verdict = session.snapshot(now)
            verdict.update(
                {
                    "counted": sample.counted,
                    "note": sample.note,
                    "skew_s": sample.skew_s,
                    "decisions": decisions,
                }
            )
            return verdict

    def evaluate(self, train_id: str, now: float) -> dict | None:
        """补齐单列车的欠拍并重估状态；无会话时返回 ``None``。"""

        with self._lock_for(train_id):
            session = self._sessions.get(train_id)
            if session is None:
                return None
            decisions = self._synthesize(session, now)
            verdict = session.snapshot(now)
            verdict["decisions"] = decisions
            return verdict

    def tick(self, now: float) -> dict:
        """控制周期：为全部会话补齐欠拍，返回按列车分组的判定快照。"""

        with self._guard:
            train_ids = sorted(self._sessions)
        report: dict[str, dict] = {}
        for train_id in train_ids:
            verdict = self.evaluate(train_id, now)
            if verdict is not None:
                report[train_id] = verdict
        return report

    def _synthesize(self, session: Session, now: float) -> list[dict]:
        """按期望周期合成欠拍采样（调用方必须已持有该列车的锁）。

        停机恢复后早于窗口的欠拍不逐拍补，合并成一条 ``collapsed`` 记录，
        保证一次长时间停机不会灌爆流水；窗口内的欠拍逐拍补齐。
        """

        tuning = self._tuning
        decisions: list[dict] = []
        last = session.last_sample_at
        if last is None:
            return decisions
        first_due = last + tuning.expected_period_s
        if first_due < now - tuning.window_s:
            skipped = (
                int(math.floor((now - tuning.window_s - first_due) / tuning.expected_period_s))
                + 1
            )
            last += skipped * tuning.expected_period_s
            sample, produced = session.ingest(
                ok=False, at=last, origin="tick", note="collapsed", count=skipped
            )
            self._journal.append({"type": "sample", **sample.as_dict()})
            decisions.extend(produced)
        while last + tuning.expected_period_s <= now + 1e-9:
            last += tuning.expected_period_s
            sample, produced = session.ingest(ok=False, at=last, origin="tick")
            self._journal.append({"type": "sample", **sample.as_dict()})
            decisions.extend(produced)
        for decision in decisions:
            self._journal.append(decision)
        return decisions

    # ---- 恢复 ---------------------------------------------------------

    def restore(self) -> int:
        """从流水重放全部采样，重建会话状态；返回重放的采样条数。"""

        records = self._journal.read_all()
        folded = 0
        for record in records:
            if not isinstance(record, dict) or record.get("type") != "sample":
                continue
            train_id = str(record.get("train_id", ""))
            if not train_id:
                continue
            with self._lock_for(train_id):
                self._session_for(train_id).fold_record(record)
            folded += 1
        return folded

    # ---- 查询 ---------------------------------------------------------

    def trust(self, train_id: str) -> str | None:
        """返回列车当前的位置可信度；无会话时返回 ``None``。"""

        with self._lock_for(train_id):
            session = self._sessions.get(train_id)
            if session is None:
                return None
            return trust_of(session.state)

    def describe(self, train_id: str, now: float) -> dict | None:
        """返回单列车链路视图；无会话时返回 ``None``。"""

        with self._lock_for(train_id):
            session = self._sessions.get(train_id)
            if session is None:
                return None
            return session.snapshot(now)

    def summary(self, now: float | None = None) -> dict:
        """汇总全部会话的链路状态。"""

        with self._guard:
            train_ids = sorted(self._sessions)
        items: dict[str, dict] = {}
        by_state = {GOOD: 0, DEGRADED: 0, LOST: 0}
        for train_id in train_ids:
            with self._lock_for(train_id):
                session = self._sessions.get(train_id)
                if session is None:
                    continue
                at = session.last_sample_at if now is None else now
                items[train_id] = session.snapshot(at if at is not None else 0.0)
                by_state[session.state] += 1
        return {
            "sessions": len(items),
            "by_state": by_state,
            "trains": items,
        }
