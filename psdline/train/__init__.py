"""列车到发。

列车保存线路、站台与当前位置，对位记录保存停稳位置与"是否已落盘"，车门状态
保存"车门已确认打开"。控制链要求"停稳对位先落盘、再开门"，因此对位与车门分成
两份文档：只有落盘的对位记录才会被开门动作接受。
"""

from __future__ import annotations

from .align import AlignRegistry, Alignment
from .door import DoorState, DoorStateRegistry
from .dwell import Dwell, DwellRegistry
from .position import PositionTracker
from .route import Route, RouteRegistry
from .state import train_state
from .train import Train, TrainRegistry

__all__ = [
    "AlignRegistry",
    "Alignment",
    "DoorState",
    "DoorStateRegistry",
    "Dwell",
    "DwellRegistry",
    "PositionTracker",
    "Route",
    "RouteRegistry",
    "Train",
    "TrainRegistry",
    "train_state",
]
