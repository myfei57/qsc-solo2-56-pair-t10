"""链路事件：只追加的哈希链事件流。

链路判定是"事后要能重放当时凭什么这么判"的关键，因此不直接覆盖任何状态，而是
把每一帧采样的接收/拒绝、每次连接代次切换、每个被采信的位置、每次放行判定与每
次副作用投递，都追加为一条事件。事件顺序号连续、每条携带前一条的 SHA-256，
任何删除、改写或插序都会在 :func:`verify_chain` 处断掉。

落盘复用 :class:`~psdline.store.JsonlJournal`（只追加、分段、fsync），哈希链在
其之上建立。
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from ..ids import IdFactory
from ..store import JsonlJournal

GENESIS = "0" * 64

# 事件类型
SAMPLE_RECEIVED = "sample_received"
SAMPLE_REJECTED = "sample_rejected"
EPOCH_CHANGED = "epoch_changed"
POSITION_APPLIED = "position_applied"
POSITION_HELD = "position_held"
STATE_CHANGED = "state_changed"
RELEASE_DECIDED = "release_decided"
RELEASE_CLOSED = "release_closed"
EFFECT_REQUESTED = "effect_requested"
EFFECT_DELIVERED = "effect_delivered"
EFFECT_FAILED = "effect_failed"
PROBE_SCHEDULED = "probe_scheduled"

REJECT_REASONS = (
    "empty_epoch",
    "stale_epoch",
    "replayed_seq",
    "out_of_order",
    "clock_skew",
    "bad_payload",
)


class TamperError(RuntimeError):
    """事件流哈希链校验失败（被删除、改写或插序）。"""


@dataclass(frozen=True)
class LinkEvent:
    """链路事件流里的一条不可变事件。"""

    seq: int
    at: float
    train_id: str
    kind: str
    payload: dict = field(default_factory=dict)
    prev_hash: str = GENESIS
    hash: str = ""

    def digest_hex(self) -> str:
        """计算本条事件的哈希（不含自身 ``hash`` 字段）。"""

        body = {
            "seq": self.seq,
            "at": self.at,
            "train_id": self.train_id,
            "kind": self.kind,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
        }
        canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "at": self.at,
            "train_id": self.train_id,
            "kind": self.kind,
            "payload": dict(self.payload),
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "LinkEvent":
        payload = raw.get("payload", {})
        return cls(
            seq=int(raw.get("seq", 0)),
            at=float(raw.get("at", 0.0)),
            train_id=str(raw.get("train_id", "")),
            kind=str(raw.get("kind", "")),
            payload=dict(payload) if isinstance(payload, dict) else {},
            prev_hash=str(raw.get("prev_hash", GENESIS)),
            hash=str(raw.get("hash", "")),
        )


class LinkEventLog:
    """全链路列车共用的一条哈希链事件流（按列车过滤）。"""

    def __init__(self, journal: JsonlJournal) -> None:
        self._journal = journal
        self._lock = threading.RLock()
        self._ids = IdFactory("linkevt")
        self._events: list[LinkEvent] = []
        self._tail = GENESIS
        self._load()

    # ---- 读取 ---------------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)

    def all(self) -> list[LinkEvent]:
        with self._lock:
            return list(self._events)

    def for_train(self, train_id: str) -> list[LinkEvent]:
        """回放一列车的事件（顺序号天然连续）。"""

        with self._lock:
            return [event for event in self._events if event.train_id == train_id]

    def tail_hash(self) -> str:
        with self._lock:
            return self._tail

    # ---- 追加 ---------------------------------------------------------

    def append(
        self,
        at: float,
        train_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
    ) -> LinkEvent:
        """原子追加一条事件并落盘。"""

        with self._lock:
            event = LinkEvent(
                seq=len(self._events) + 1,
                at=float(at),
                train_id=str(train_id),
                kind=kind,
                payload=dict(payload or {}),
                prev_hash=self._tail,
            )
            object.__setattr__(event, "hash", event.digest_hex())
            self._journal.append(event.as_dict())
            self._events.append(event)
            self._tail = event.hash
            return event

    def append_many(self, records: list[tuple[float, str, str, dict | None]]) -> list[LinkEvent]:
        """在同一把锁内连续追加，保证一组"判定+副作用"不被并发刷新切开。"""

        with self._lock:
            return [self.append(at, train_id, kind, payload) for at, train_id, kind, payload in records]

    def find(self, event_seq: int) -> LinkEvent | None:
        with self._lock:
            if 1 <= event_seq <= len(self._events):
                return self._events[event_seq - 1]
            return None

    # ---- 落盘与校验 ---------------------------------------------------

    def _load(self) -> None:
        """启动时从落盘流水重建内存链，并当场校验完整性。"""

        records = self._journal.read_all()
        events = [LinkEvent.from_dict(raw) for raw in records if isinstance(raw, dict)]
        verify_events(events)
        self._events = events
        self._tail = events[-1].hash if events else GENESIS

    def verify(self) -> dict:
        with self._lock:
            verify_events(self._events)
            return {"events": len(self._events), "tail": self._tail, "ok": True}


def verify_events(events: list[LinkEvent], on_verify: Callable[[LinkEvent], None] | None = None) -> None:
    """校验事件顺序号连续且哈希链完整。"""

    previous = GENESIS
    for index, event in enumerate(events, start=1):
        if event.seq != index:
            raise TamperError(f"事件顺序号不连续：期望 {index}，实际 {event.seq}")
        if event.prev_hash != previous:
            raise TamperError(f"事件 {event.seq} 前向哈希断裂（被插序或改写）")
        if event.digest_hex() != event.hash:
            raise TamperError(f"事件 {event.seq} 内容哈希不匹配（被改写）")
        previous = event.hash
        if on_verify is not None:
            on_verify(event)
