"""链路判定的副作用外发与失败恢复。

判定（落事件）与外发（驱动信号/门等现场设备）分开：先把"要做什么"作为不可变事件
落盘，再由外发箱投递。投递是**幂等**的——同一个 ``effect_id`` 重试任意次，对接收
侧只产生一次效果。进程在"落盘之后、投递成功之前"崩溃，重启时 :meth:`redrive` 会
把事件流里所有"已请求但未投递成功"的副作用重投，因此支持部分失败恢复。

现场外发目标由 :class:`LinkSink` 抽象，默认实现只记录（供演示与测试），可注入会
失败的实现来验证重投。
"""

from __future__ import annotations

import threading
from typing import Callable, Protocol

from .eventlog import (
    EFFECT_DELIVERED,
    EFFECT_FAILED,
    EFFECT_REQUESTED,
    LinkEventLog,
)


class LinkSink(Protocol):
    """链路判定副作用的现场接收侧（信号机/门控/告警）。"""

    def deliver(self, effect_id: str, kind: str, payload: dict) -> None:
        """幂等地施加一次效果；失败时抛异常。"""


class RecordingSink:
    """记录全部已施加效果的测试/演示接收侧，按 ``effect_id`` 幂等。"""

    def __init__(self) -> None:
        self._delivered: dict[str, tuple[str, dict]] = {}
        self._lock = threading.RLock()
        self.fail_on: Callable[[str, str, dict], bool] | None = None
        self.attempts = 0

    def deliver(self, effect_id: str, kind: str, payload: dict) -> None:
        with self._lock:
            self.attempts += 1
            if self.fail_on is not None and self.fail_on(effect_id, kind, payload):
                raise RuntimeError(f"模拟现场设备失败: {kind}")
            # 幂等：同一个 effect_id 已施加过则直接视为成功，不重复动作。
            self._delivered.setdefault(effect_id, (kind, dict(payload)))

    def effects(self) -> list[dict]:
        with self._lock:
            return [
                {"effect_id": effect_id, "kind": kind, **payload}
                for effect_id, (kind, payload) in sorted(self._delivered.items())
            ]

    def kinds(self) -> list[str]:
        with self._lock:
            return sorted({kind for kind, _payload in self._delivered.values()})

    def reset(self) -> None:
        with self._lock:
            self._delivered.clear()
            self.attempts = 0
            self.fail_on = None


class EffectOutbox:
    """把事件流中的副作用请求投递给接收侧，失败可重投。"""

    def __init__(self, log: LinkEventLog, sinks: dict[str, LinkSink] | None = None) -> None:
        self._log = log
        self._sinks: dict[str, LinkSink] = dict(sinks or {})
        self._lock = threading.RLock()

    def attach(self, kind: str, sink: LinkSink) -> None:
        with self._lock:
            self._sinks[kind] = sink

    def redrive(self, train_id: str, pending: dict[str, dict], now: float) -> dict:
        """重投一列车未确认的副作用（启动恢复与周期补偿共用）。

        ``pending`` 来自状态折叠：``{effect_id: {kind, payload}}``。
        """

        delivered: list[str] = []
        failed: list[str] = []
        for effect_id in sorted(pending):
            record = pending[effect_id]
            kind = str(record.get("kind", ""))
            if self._deliver_one(
                effect_id, kind,
                {"train_id": train_id, **dict(record.get("payload", {}))}, now
            ):
                delivered.append(effect_id)
            else:
                failed.append(effect_id)
        return {"delivered": delivered, "failed": failed, "retried": len(pending)}

    def _deliver_one(self, effect_id: str, kind: str, payload: dict, now: float) -> bool:
        with self._lock:
            sink = self._sinks.get(kind)
            if sink is None:
                # 没有接收侧（纯判定场景）：直接标记投递成功，避免永远挂起。
                self._log.append(now, str(payload.get("train_id", "")), EFFECT_DELIVERED,
                                 {"effect_id": effect_id, "kind": kind, "sink": "none"})
                return True
            try:
                sink.deliver(effect_id, kind, payload)
            except Exception as error:  # 现场设备失败：留待退避后重投
                self._log.append(now, str(payload.get("train_id", "")), EFFECT_FAILED,
                                 {"effect_id": effect_id, "kind": kind, "error": str(error)})
                return False
            self._log.append(now, str(payload.get("train_id", "")), EFFECT_DELIVERED,
                             {"effect_id": effect_id, "kind": kind})
            return True

    @staticmethod
    def request_record(
        at: float, train_id: str, effect_id: str, kind: str, payload: dict
    ) -> tuple[float, str, str, dict]:
        """构造一条副作用请求事件记录，供调用方与判定事件**同批**追加。"""

        return (
            at,
            train_id,
            EFFECT_REQUESTED,
            {"effect_id": effect_id, "kind": kind, "payload": payload},
        )

    def deliver_record(self, at: float, train_id: str, record: dict) -> bool:
        """投递一条已落盘的副作用请求（幂等、失败留痕）。"""

        effect_id = str(record.get("effect_id", ""))
        kind = str(record.get("kind", ""))
        payload = dict(record.get("payload", {}))
        return self._deliver_one(effect_id, kind, {"train_id": train_id, **payload}, at)
