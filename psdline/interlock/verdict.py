"""门控事件序号判定。

门控事件序号是 16 位计数器，每 65536 条回绕一次。判定"下一序号是否在本周期内
单调前进"时必须按模运算算增量：增量落在 ``(0, 32768)`` 才算前进，超过半圈说明
是回绕后的大跳变或序号被重放，直接拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass

MODULUS = 65536
HALF = MODULUS // 2


def next_sequence(previous: int, step: int) -> int:
    """按模运算推进序号。"""

    return (int(previous) + int(step)) % MODULUS


@dataclass(frozen=True)
class SequenceVerdict:
    """一次序号判定的结果。"""

    previous: int
    current: int
    delta: int
    advanced: bool
    reason: str

    def as_dict(self) -> dict:
        return {
            "previous": self.previous,
            "current": self.current,
            "delta": self.delta,
            "advanced": self.advanced,
            "reason": self.reason,
        }


def verdict_of(previous: int, current: int) -> SequenceVerdict:
    """判定序号是否在本周期内前进。"""

    previous %= MODULUS
    current %= MODULUS
    delta = (current - previous) % MODULUS
    if delta == 0:
        return SequenceVerdict(previous, current, delta, False, "replay")
    if delta >= HALF:
        return SequenceVerdict(previous, current, delta, False, "jump_backwards")
    return SequenceVerdict(previous, current, delta, True, "advanced")
