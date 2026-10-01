"""站台门设备。

站台门保存所属站台与门区、开闭状态与锁定状态。开门由门控动作驱动，锁定由联锁
按条件解除；清扫（``sweep``）把整站门区收回关闭位，用于交接班前的复位。锁定状态
单独提供读写入口，联锁不直接改开闭标志，避免两处状态互相覆盖。
"""

from __future__ import annotations

from .lockout import LockoutRegistry
from .psd import PSD, PSDRegistry
from .status import gate_status, station_status
from .sweep import sweep_station

__all__ = [
    "LockoutRegistry",
    "PSD",
    "PSDRegistry",
    "gate_status",
    "station_status",
    "sweep_station",
]
