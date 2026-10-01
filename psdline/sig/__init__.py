"""信号联动。

信号侧维护"到站""发车"两个灯位：到站信号先到、停稳对位后再开门；发车信号必须
在间隙探测确认且关门已落盘之后才允许升起。位置刷新也走这一层，让列车位置只由
信号源写入。
"""

from __future__ import annotations

from .aspect import aspect_of
from .depart import close_durable, depart_checked
from .refresh import refresh_position
from .sig import SignalController
from .status import signal_status, signal_summary

__all__ = [
    "SignalController",
    "aspect_of",
    "close_durable",
    "depart_checked",
    "refresh_position",
    "signal_status",
    "signal_summary",
]
