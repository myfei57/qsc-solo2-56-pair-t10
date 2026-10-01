"""门控联锁。

联锁负责三件事：故障置位时把对应门区锁在关闭位、按条件解除锁定、以及判定门控
事件序号是否在一个周期内单调递增（序号回绕时不能把"跨周期"误判成"跳变"）。
锁定状态不写在联锁自己的文档里，而是落到站台门上，保证只有一份事实。
"""

from __future__ import annotations

from .arm import arm_key, InterlockArmer
from .clear import release_ready, releasable
from .interlock import ACTIVE, CLEAR, FaultRegistry
from .restore import restore_lockouts
from .status import interlock_status
from .verdict import MODULUS, SequenceVerdict, next_sequence, verdict_of

__all__ = [
    "ACTIVE",
    "CLEAR",
    "FaultRegistry",
    "InterlockArmer",
    "MODULUS",
    "SequenceVerdict",
    "arm_key",
    "interlock_status",
    "next_sequence",
    "release_ready",
    "releasable",
    "restore_lockouts",
    "verdict_of",
]
