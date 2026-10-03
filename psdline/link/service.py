"""链路质量服务：把采样、位置可信度、放行闸门、副作用与审计串成一条链。

并发与一致性：

* **按列车串行**：每列车一把锁，同一列车的"采样接收 / 放行判定 / 人工确认 /
  周期推进"互斥，杜绝并发刷新把状态和判定写花；不同列车互不阻塞。
* **判定与副作用原子落盘**：一个判定和它要下发的副作用请求，在同一批
  :meth:`LinkEventLog.append_many` 中追加，要么都在、要么都不在。
* **掉线重连**：车载侧换新 ``epoch``；旧代次的迟到帧一律丢弃。新代次首批好采样
  之前处于"重连确认中"，可信度不瞬间回升。
* **乱序/重复**：同一代次内帧序号必须严格递增；回放、回跳、重复帧均拒绝。
* **时钟漂移**：车载时钟与地面接收时刻偏差超配置即拒绝，绝不拿穿越帧刷新位置。
* **部分失败恢复**：副作用幂等且"先登记后投递"，启动时重放事件流把所有未确认
  副作用重投（:meth:`redrive_pending`）。
"""

from __future__ import annotations

import threading
from typing import Callable

from ..clock import Clock
from ..config import LinkSection
from ..eventbus import Event, EventBus
from . import state as states
from .effects import EffectOutbox
from .eventlog import (
    EPOCH_CHANGED,
    POSITION_APPLIED,
    POSITION_HELD,
    PROBE_SCHEDULED,
    RELEASE_CLOSED,
    RELEASE_DECIDED,
    SAMPLE_RECEIVED,
    SAMPLE_REJECTED,
    STATE_CHANGED,
    LinkEvent,
    LinkEventLog,
)
from .gate import (
    DECISION_AUTO,
    DECISION_CLOSED,
    DECISION_EMERGENCY,
    DECISION_INHIBITED,
    DECISION_MANUAL_ARMED,
    DECISION_MANUAL_PENDING,
    EFFECT_ALARM,
    EFFECT_ARM_DOORS,
    EFFECT_LOCK_DOORS,
    EFFECT_SIGNAL_GREEN,
    EFFECT_SIGNAL_HOLD,
    GATE_INACTIVE,
    LinkGate,
)
from .policy import POLICY_VERSION, LinkPolicy
from .sample import LinkSample, sample_score

# 位置可信度达到多少才允许把该帧位置写入位置文档（降级时位置冻结并显式标记）
POSITION_TRUST_RULE = "position_trust_fresh"
POSITION_HOLD_RULE = "position_hold_degraded"


class LinkService:
    """链路质量、可信度与降级放行的编排服务。"""

    def __init__(
        self,
        section: LinkSection,
        clock: Clock,
        log: LinkEventLog,
        bus: EventBus,
        outbox: EffectOutbox | None = None,
    ) -> None:
        self._s = section
        self._clock = clock
        self._log = log
        self._bus = bus
        self._policy = LinkPolicy(section)
        self._gate = LinkGate(section)
        self._outbox = outbox or EffectOutbox(log)
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._request_seq = 0
        # 有界乱序接收窗口：一个代次内容忍网络/并发导致的有界交错，只有序号低于
        # 接收下界才判为乱序丢弃；与当前最大序号相同（或窗口内重复）判为重放。
        self._reorder_window = 256
        self._trains: set[str] = set()
        for event in log.all():
            self._trains.add(event.train_id)

    @property
    def policy(self) -> LinkPolicy:
        return self._policy

    @property
    def gate(self) -> LinkGate:
        return self._gate

    @property
    def outbox(self) -> EffectOutbox:
        return self._outbox

    def adopted_trains(self) -> list[str]:
        """已纳入链路管控（出现过已接收采样）的列车。"""

        return sorted(train for train in self._trains if train)

    def is_adopted(self, train_id: str) -> bool:
        return train_id in self._trains

    def _lock(self, train_id: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(train_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[train_id] = lock
            return lock

    # ---- 视图 ---------------------------------------------------------

    def _fold(self, train_id: str) -> states.LinkState:
        return states.fold(self._log.for_train(train_id), train_id)

    def view(self, train_id: str, at: float | None = None) -> dict:
        now = self._clock.now() if at is None else at
        with self._lock(train_id):
            return self._fold(train_id).view(now, self._policy)

    def views(self, at: float | None = None) -> dict[str, dict]:
        now = self._clock.now() if at is None else at
        result: dict[str, dict] = {}
        for train_id in self.adopted_trains():
            with self._lock(train_id):
                result[train_id] = self._fold(train_id).view(now, self._policy)
        return result

    # ---- 采样接收 -----------------------------------------------------

    def ingest(self, sample: LinkSample) -> dict:
        """接收一帧链路采样，完成校验、状态推进、位置采信与放行联动。

        返回该帧是否被采信、链路分、当前可信度，以及位置是否被写入。
        """

        at = self._clock.now()
        train_id = sample.train_id
        with self._lock(train_id):
            current = self._fold(train_id)

            rejection = self._validate(sample, current, at)
            if rejection is not None:
                reason, detail = rejection
                self._log.append(
                    at, train_id, SAMPLE_REJECTED,
                    {"reason": reason, "seq": sample.seq, "epoch": sample.epoch, **detail},
                )
                self._bus.publish(Event.of("link", "link", "sample_rejected",
                                           train_id=train_id, reason=reason, severity="alarm"))
                return {"accepted": False, "reason": reason, "train_id": train_id}

            score = sample_score(sample, self._policy)
            good = score >= self._s.sample_good_score

            records: list[tuple] = []
            epoch_changed = current.epoch and sample.epoch != current.epoch
            first_contact = not current.epoch
            if first_contact or epoch_changed:
                from_lost = self._was_in_lost(current, at)
                records.append((
                    at, train_id, EPOCH_CHANGED,
                    {"epoch": sample.epoch, "from_epoch": current.epoch, "from_lost": from_lost},
                ))
            records.append((
                at, train_id, SAMPLE_RECEIVED,
                {"seq": sample.seq, "epoch": sample.epoch, "score": score, "good": good,
                 "sample": sample.as_dict(), "policy": POLICY_VERSION},
            ))

            # 先原子落盘"代次切换 + 采样接收"，再用真实事件折叠出当前视图，
            # 据此决定本帧位置是采信还是扣留（判定依据即本帧之后的链路状态）。
            base_events = self._log.append_many(records)
            self._trains.add(train_id)
            projected = states.fold(self._log.for_train(train_id), train_id)
            view = projected.view(at, self._policy)

            # 重连恢复：连续好采样攒够后，落一条恢复事件清除"确认中"，再取视图。
            if (
                projected.was_lost
                and projected.consecutive_good >= self._s.recover_good_samples
                and view["contact"] in (states.OK, states.MARGINAL)
            ):
                self._log.append(
                    at, train_id, STATE_CHANGED,
                    {"lost": False, "backoff_level": 0, "recovered": True,
                     "consecutive_good": projected.consecutive_good},
                )
                self._bus.publish(Event.of("link", "link", "link_recovered",
                                           train_id=train_id, severity="notice"))
                projected = states.fold(self._log.for_train(train_id), train_id)
                view = projected.view(at, self._policy)

            position_records: list[tuple] = []
            position_written = False
            if sample.position_m is not None:
                if self._position_trusted(view, frame_good=good):
                    position_records.append((
                        at, train_id, POSITION_APPLIED,
                        {"position_m": sample.position_m, "seq": sample.seq,
                         "epoch": sample.epoch, "confidence": view["confidence"],
                         "contact": view["contact"]},
                    ))
                    position_written = True
                else:
                    position_records.append((
                        at, train_id, POSITION_HELD,
                        {"position_m": sample.position_m, "seq": sample.seq,
                         "confidence": view["confidence"], "contact": view["contact"],
                         "rule": POSITION_HOLD_RULE},
                    ))
            if position_records:
                self._log.append_many(position_records)

            # 状态/退避联动（可能再产生状态事件），并对开放中的放行请求做再判定。
            self._advance_state(train_id, at)
            self._reevaluate_open(train_id, at)

            self._bus.publish(Event.of("link", "link", "sample_received",
                                       train_id=train_id, score=score, good=good,
                                       confidence=view["confidence"], severity="notice"))
            return {
                "accepted": True,
                "train_id": train_id,
                "epoch": sample.epoch,
                "seq": sample.seq,
                "score": score,
                "good": good,
                "confidence": view["confidence"],
                "contact": view["contact"],
                "position_m": sample.position_m if position_written else None,
                "position_written": position_written,
                "epoch_changed": bool(first_contact or epoch_changed),
            }

    def _validate(self, sample: LinkSample, current: states.LinkState, at: float) -> tuple[str, dict] | None:
        if not sample.train_id:
            return "bad_payload", {"detail": "train_id 为空"}
        if not sample.epoch:
            return "empty_epoch", {}
        # 时钟漂移先判：穿越帧无论属于哪个代次都不能采信。
        if self._policy.skew_s(sample.sent_at, at) > self._s.clock_skew_s:
            return "clock_skew", {"skew_s": round(at - sample.sent_at, 3),
                                  "limit_s": self._s.clock_skew_s}
        if not current.epoch:
            return None  # 该列车首帧：接受并建立代次
        if sample.epoch == current.epoch:
            # 有界乱序窗口：窗口内/已接收的序号属重放；低于下界才算乱序旧帧；
            # 其余（包括交错到达的更大/更小但仍在窗口内的新帧）正常接收。
            if sample.seq in current.seen_seqs:
                return "replayed_seq", {"max_seq": current.max_seq}
            lower = current.max_seq - self._reorder_window
            if sample.seq <= lower:
                return "out_of_order", {"max_seq": current.max_seq,
                                        "window": self._reorder_window}
            return None
        if sample.epoch in current.epochs_seen:
            # 旧代次的迟到帧（掉线重连后旧连接残包）：丢弃，绝不刷新状态。
            return "stale_epoch", {"current_epoch": current.epoch}
        return None  # 全新代次（掉线重连）：交由 EPOCH_CHANGED 处理

    def _was_in_lost(self, current: states.LinkState, at: float) -> bool:
        if current.last_seen_at is None:
            return False
        return (at - current.last_seen_at) > self._s.loss_s

    def _position_trusted(self, view: dict, frame_good: bool | None = None) -> bool:
        """位置是否可采信。

        要求同时满足：接触状态正常（良好/勉强/重连确认中）、帧在新鲜窗口内、
        可信度高于手动阈值、且本帧自身链路分不低于单采样合格线。最后一条保证
        "刚到达但质量极差"的帧也不能借新鲜窗口把位置冲掉。
        """

        if view["contact"] not in (states.OK, states.MARGINAL, states.REACQUIRING):
            return False
        if view["last_seen_age_s"] is None or view["last_seen_age_s"] > self._s.fresh_s:
            return False
        if view["confidence"] < self._s.confidence_manual:
            return False
        if frame_good is False:
            return False
        return True

    # ---- 周期推进（退避/失联/再判定）---------------------------------

    def tick(self, train_id: str, at: float | None = None) -> dict:
        """推进一列车一个控制周期：失联退避升级、探测调度、开放请求再判定。"""

        now = self._clock.now() if at is None else at
        with self._lock(train_id):
            return self._advance_state(train_id, now, tick=True)

    def tick_all(self, at: float | None = None) -> dict:
        now = self._clock.now() if at is None else at
        summary = {}
        for train_id in self.adopted_trains():
            with self._lock(train_id):
                summary[train_id] = self._advance_state(train_id, now, tick=True)
        return summary

    def _advance_state(self, train_id: str, at: float, tick: bool = False) -> dict:
        current = self._fold(train_id)
        if current.last_seen_at is None:
            return {"contact": states.UNKNOWN, "backoff_level": 0}
        age = at - current.last_seen_at
        view = current.view(at, self._policy)
        lost = view["contact"] == states.LOST

        records: list[tuple] = []
        new_level = current.backoff_level
        if lost:
            # 超过 loss_s 起按断线时长升级退避档，并在每档安排一次重连探测。
            target_level = self._backoff_band(age)
            if target_level != current.backoff_level and target_level > current.backoff_level:
                new_level = target_level
                records.append((
                    at, train_id, STATE_CHANGED,
                    {"lost": True, "backoff_level": new_level, "age_s": round(age, 3),
                     "next_delay_s": self._policy.backoff_delay(new_level, train_id, current.probes + 1)},
                ))
                records.append((
                    at, train_id, PROBE_SCHEDULED,
                    {"backoff_level": new_level,
                     "delay_s": self._policy.backoff_delay(new_level, train_id, current.probes + 1)},
                ))
                self._bus.publish(Event.of("link", "link", "link_lost",
                                           train_id=train_id, backoff_level=new_level,
                                           age_s=round(age, 3), severity="alarm"))
        elif view["contact"] in (states.OK,) and current.was_lost is False and current.backoff_level:
            new_level = 0
            records.append((at, train_id, STATE_CHANGED,
                            {"lost": False, "backoff_level": 0}))

        if records:
            self._log.append_many(records)
        return {"contact": view["contact"], "backoff_level": new_level,
                "confidence": view["confidence"], "ticked": tick}

    def _backoff_band(self, age_s: float) -> int:
        """按断线时长落退避档：loss_s 后每翻一个 factor 时长升一档。"""

        excess = max(0.0, age_s - self._s.loss_s)
        if excess <= 0:
            return 0
        level = 0
        boundary = self._s.backoff_base_s
        while excess >= boundary and level < 64:
            level += 1
            boundary *= self._s.backoff_factor
        return max(1, level)

    # ---- 发车放行 -----------------------------------------------------

    def request_release(self, train_id: str, station_id: str, at: float | None = None) -> dict:
        """对一次发车（含开门联锁）作出放行判定并下发对应副作用。"""

        now = self._clock.now() if at is None else at
        with self._lock(train_id):
            adopted = self.is_adopted(train_id)
            view = self._fold(train_id).view(now, self._policy)
            verdict = self._gate.evaluate(view, adopted=adopted)
            self._request_seq += 1
            request_id = f"rel-{train_id}-{self._request_seq:06d}"
            evidence = self._evidence(view, verdict)

            records: list = []
            if verdict.decision == DECISION_AUTO:
                open_effect = self._effect_id(train_id, request_id, "open")
                records.extend(self._decision_records(
                    now, train_id, request_id, station_id, DECISION_AUTO, verdict.rule, evidence))
                records.append(self._outbox.request_record(
                    now, train_id, open_effect, EFFECT_SIGNAL_GREEN,
                    {"station_id": station_id, "request_id": request_id}))
                records.append(self._outbox.request_record(
                    now, train_id, self._effect_id(train_id, request_id, "arm"),
                    EFFECT_ARM_DOORS, {"station_id": station_id, "request_id": request_id}))
            elif verdict.decision == DECISION_MANUAL_PENDING:
                records.extend(self._decision_records(
                    now, train_id, request_id, station_id, DECISION_MANUAL_PENDING,
                    verdict.rule, evidence))
                records.append(self._outbox.request_record(
                    now, train_id, self._effect_id(train_id, request_id, "lock"),
                    EFFECT_LOCK_DOORS, {"station_id": station_id, "request_id": request_id}))
                records.append(self._outbox.request_record(
                    now, train_id, self._effect_id(train_id, request_id, "hold"),
                    EFFECT_SIGNAL_HOLD, {"station_id": station_id, "request_id": request_id}))
                records.append(self._outbox.request_record(
                    now, train_id, self._effect_id(train_id, request_id, "alarm"),
                    EFFECT_ALARM, {"station_id": station_id, "request_id": request_id,
                                   "level": "degraded"}))
            else:  # inhibited
                records.extend(self._decision_records(
                    now, train_id, request_id, station_id, DECISION_INHIBITED,
                    verdict.rule, evidence))
                records.append(self._outbox.request_record(
                    now, train_id, self._effect_id(train_id, request_id, "lock"),
                    EFFECT_LOCK_DOORS, {"station_id": station_id, "request_id": request_id}))
                records.append(self._outbox.request_record(
                    now, train_id, self._effect_id(train_id, request_id, "alarm"),
                    EFFECT_ALARM, {"station_id": station_id, "request_id": request_id,
                                   "level": "lost"}))

            appended = self._log.append_many(records)
            # 判定与副作用请求已原子落盘；随后投递，失败留待 redrive 补偿。
            self._deliver_requested(appended, now)

            self._bus.publish(Event.of(
                "link", "release", verdict.decision, train_id=train_id,
                station_id=station_id, request_id=request_id, rule=verdict.rule,
                severity="notice" if verdict.decision == DECISION_AUTO else "alarm"))

            return {
                "ok": verdict.decision == DECISION_AUTO,
                "train_id": train_id,
                "station_id": station_id,
                "request_id": request_id,
                "verdict": verdict.as_dict(),
                "mode": verdict.mode,
                "decision": verdict.decision,
                "rule": verdict.rule,
                "reason": verdict.reason,
                "confirmed": False,
                "evidence": evidence,
                "violations": [] if verdict.decision == DECISION_AUTO else [verdict.reason],
            }

    def confirm_release(
        self, train_id: str, request_id: str, operator: str, at: float | None = None
    ) -> dict:
        """值班员对一次降级（manual）发车显式确认：当次放开开门与发车。"""

        now = self._clock.now() if at is None else at
        with self._lock(train_id):
            open_request = self._require_open(train_id, request_id)
            if open_request["decision"] != DECISION_MANUAL_PENDING:
                return self._confirm_error(train_id, request_id, operator,
                                           "该放行请求不处于待人工确认状态", open_request, now)
            view = self._fold(train_id).view(now, self._policy)
            # 确认的当下仍必须是降级档；确认期间若彻底失联，确认无效（fail-safe）。
            if view["mode"] != states.MANUAL:
                return self._confirm_error(train_id, request_id, operator,
                                           f"链路状态已变为 {view['contact']}，人工确认失效",
                                           open_request, now)

            records = list(self._decision_records(
                now, train_id, request_id, open_request.get("station_id", ""),
                DECISION_MANUAL_ARMED, "manual_confirmed", self._evidence(
                    view, self._gate.evaluate(view, True)),
                extra={"operator": operator, "replaces": DECISION_MANUAL_PENDING}))
            arm_effect = self._effect_id(train_id, request_id, "arm")
            green_effect = self._effect_id(train_id, request_id, "green")
            records.append(self._outbox.request_record(
                now, train_id, arm_effect, EFFECT_ARM_DOORS,
                {"station_id": open_request.get("station_id", ""), "request_id": request_id,
                 "operator": operator, "manual": True}))
            records.append(self._outbox.request_record(
                now, train_id, green_effect, EFFECT_SIGNAL_GREEN,
                {"station_id": open_request.get("station_id", ""), "request_id": request_id,
                 "operator": operator, "manual": True}))
            appended = self._log.append_many(records)
            self._deliver_requested(appended, now)
            self._bus.publish(Event.of("link", "release", "manual_confirmed",
                                       train_id=train_id, request_id=request_id,
                                       operator=operator, severity="alarm"))
            return {
                "ok": True, "train_id": train_id, "request_id": request_id,
                "decision": DECISION_MANUAL_ARMED, "rule": "manual_confirmed",
                "operator": operator, "confirmed": True,
                "reason": "值班员已对本次降级发车显式确认", "violations": [],
            }

    def emergency_override(
        self, train_id: str, station_id: str, supervisor: str, reason: str,
        at: float | None = None,
    ) -> dict:
        """高权限紧急覆盖：彻底失联时旁路禁止态（强审计，双人责任留痕）。"""

        now = self._clock.now() if at is None else at
        if not supervisor or not reason:
            return {"ok": False, "train_id": train_id, "decision": DECISION_EMERGENCY,
                    "violations": ["紧急覆盖必须填写责任人和理由"]}
        with self._lock(train_id):
            self._request_seq += 1
            request_id = f"emg-{train_id}-{self._request_seq:06d}"
            view = self._fold(train_id).view(now, self._policy)
            verdict = self._gate.evaluate(view, adopted=self.is_adopted(train_id))
            records = list(self._decision_records(
                now, train_id, request_id, station_id, DECISION_EMERGENCY,
                "emergency_override", self._evidence(view, verdict),
                extra={"supervisor": supervisor, "reason": reason}))
            records.append(self._outbox.request_record(
                now, train_id, self._effect_id(train_id, request_id, "arm"),
                EFFECT_ARM_DOORS, {"station_id": station_id, "request_id": request_id,
                                   "supervisor": supervisor, "reason": reason, "emergency": True}))
            records.append(self._outbox.request_record(
                now, train_id, self._effect_id(train_id, request_id, "green"),
                EFFECT_SIGNAL_GREEN, {"station_id": station_id, "request_id": request_id,
                                      "supervisor": supervisor, "reason": reason, "emergency": True}))
            appended = self._log.append_many(records)
            self._deliver_requested(appended, now)
            self._bus.publish(Event.of("link", "release", "emergency_override",
                                       train_id=train_id, station_id=station_id,
                                       request_id=request_id, supervisor=supervisor,
                                       severity="alarm"))
            return {
                "ok": True, "train_id": train_id, "station_id": station_id,
                "request_id": request_id, "decision": DECISION_EMERGENCY,
                "rule": "emergency_override", "supervisor": supervisor,
                "reason": reason, "evidence": self._evidence(view, verdict), "violations": [],
            }

    def close_release(self, train_id: str, request_id: str, at: float | None = None) -> dict:
        """发车完成后关闭一次放行请求（回收开门联锁授权）。"""

        now = self._clock.now() if at is None else at
        with self._lock(train_id):
            self._require_open(train_id, request_id)
            self._log.append(now, train_id, RELEASE_CLOSED,
                             {"request_id": request_id, "decision": DECISION_CLOSED})
            self._bus.publish(Event.of("link", "release", "closed",
                                       train_id=train_id, request_id=request_id,
                                       severity="notice"))
            return {"ok": True, "train_id": train_id, "request_id": request_id,
                    "decision": DECISION_CLOSED, "violations": []}

    def can_open_doors(self, train_id: str, at: float | None = None) -> tuple[bool, str, bool]:
        """开门联锁查询：返回 (是否允许开门, 规则, 是否纳入管控)。"""

        now = self._clock.now() if at is None else at
        with self._lock(train_id):
            adopted = self.is_adopted(train_id)
            state = self._fold(train_id)
            view = state.view(now, self._policy)
            confirmed = (
                state.open_request is not None
                and state.open_request.get("decision") == DECISION_MANUAL_ARMED
            )
            allowed, rule = self._gate.can_open_doors(view, confirmed, adopted=adopted)
            return allowed, rule, adopted

    # ---- 开放请求的随链路恢复再判定 -----------------------------------

    def _reevaluate_open(self, train_id: str, at: float) -> None:
        current = self._fold(train_id)
        open_request = current.open_request
        if open_request is None:
            return
        # 已人工确认放行的，不在采样恢复路径上撤销；待确认的随状态自动升降档。
        if open_request.get("decision") != DECISION_MANUAL_PENDING:
            return
        view = current.view(at, self._policy)
        if view["mode"] == states.AUTO:
            # 链路恢复到自动档：把待确认请求提升为自动放行（无需再人工确认）。
            records = list(self._decision_records(
                at, train_id, open_request["request_id"],
                open_request.get("station_id", ""), DECISION_AUTO,
                "recovered_auto", self._evidence(view, self._gate.evaluate(view, True)),
                extra={"replaces": DECISION_MANUAL_PENDING}))
            records.append(self._outbox.request_record(
                at, train_id, self._effect_id(train_id, open_request["request_id"], "green"),
                EFFECT_SIGNAL_GREEN,
                {"station_id": open_request.get("station_id", ""),
                 "request_id": open_request["request_id"], "recovered": True}))
            appended = self._log.append_many(records)
            self._deliver_requested(appended, at)
            self._bus.publish(Event.of("link", "release", "recovered_auto",
                                       train_id=train_id,
                                       request_id=open_request["request_id"],
                                       severity="notice"))

    # ---- 部分失败恢复 -------------------------------------------------

    def redrive_pending(self, at: float | None = None) -> dict:
        """重放事件流，把所有未确认投递的副作用重投（启动恢复/周期补偿）。"""

        now = self._clock.now() if at is None else at
        total = {"delivered": [], "failed": [], "retried": 0}
        for train_id in self.adopted_trains():
            with self._lock(train_id):
                pending = self._fold(train_id).pending_effects
                if not pending:
                    continue
                # 取出原始请求负载（重放时从事件里还原 payload/kind）。
                records = {
                    effect_id: self._effect_payload(train_id, effect_id)
                    for effect_id in pending
                }
                records = {key: value for key, value in records.items() if value is not None}
                outcome = self._outbox.redrive(train_id, records, now)
                total["delivered"].extend(outcome["delivered"])
                total["failed"].extend(outcome["failed"])
                total["retried"] += outcome["retried"]
        return total

    def _effect_payload(self, train_id: str, effect_id: str) -> dict | None:
        for event in self._log.for_train(train_id):
            if event.kind == "effect_requested" and event.payload.get("effect_id") == effect_id:
                return {
                    "effect_id": effect_id,
                    "kind": event.payload.get("kind", ""),
                    "payload": event.payload.get("payload", {}),
                }
        return None

    # ---- 内部工具 -----------------------------------------------------

    def _decision_records(
        self, at: float, train_id: str, request_id: str, station_id: str,
        decision: str, rule: str, evidence: dict, extra: dict | None = None
    ) -> list[tuple]:
        payload = {
            "request_id": request_id,
            "station_id": station_id,
            "decision": decision,
            "rule": rule,
            "policy_version": POLICY_VERSION,
            "thresholds": self._policy.thresholds,
            "evidence": evidence,
        }
        if extra:
            payload.update(extra)
        return [(at, train_id, RELEASE_DECIDED, payload)]

    def _evidence(self, view: dict, verdict) -> dict:
        """固化"凭什么这么判"的全部依据，随事件落盘。"""

        return {
            "contact": view["contact"],
            "mode": view["mode"],
            "confidence": view["confidence"],
            "window_score": view["window_score"],
            "window_samples": view["window_samples"],
            "last_seen_age_s": view["last_seen_age_s"],
            "position_m": view["position_m"],
            "position_age_s": view["position_age_s"],
            "epoch": view["epoch"],
            "last_seq": view["last_seq"],
            "consecutive_good": view["consecutive_good"],
            "recover_required": view["recover_required"],
            "rule": verdict.rule,
            "thresholds": {
                "fresh_s": self._s.fresh_s,
                "stale_s": self._s.stale_s,
                "loss_s": self._s.loss_s,
                "confidence_auto": self._s.confidence_auto,
                "confidence_manual": self._s.confidence_manual,
                "recover_good_samples": self._s.recover_good_samples,
                "window_good_score": self._s.window_good_score,
            },
        }

    def _effect_id(self, train_id: str, request_id: str, tag: str) -> str:
        return f"eff-{request_id}-{tag}"

    def _require_open(self, train_id: str, request_id: str) -> dict:
        open_request = self._fold(train_id).open_request
        if open_request is None or open_request.get("request_id") != request_id:
            raise ValueError(f"没有处于开放状态的放行请求: {request_id}")
        return open_request

    def _confirm_error(self, train_id, request_id, operator, message, open_request, now) -> dict:
        self._log.append(now, train_id, RELEASE_DECIDED,
                         {"request_id": request_id, "decision": "confirm_rejected",
                          "rule": open_request.get("decision", ""),
                          "operator": operator, "reason": message})
        self._bus.publish(Event.of("link", "release", "confirm_rejected",
                                   train_id=train_id, request_id=request_id,
                                   operator=operator, severity="alarm"))
        return {"ok": False, "train_id": train_id, "request_id": request_id,
                "decision": open_request.get("decision"), "operator": operator,
                "confirmed": False, "violations": [message]}

    def _deliver_requested(self, appended: list[LinkEvent], at: float) -> None:
        for event in appended:
            if event.kind != "effect_requested":
                continue
            self._outbox.deliver_record(event.at, event.train_id, event.payload)
