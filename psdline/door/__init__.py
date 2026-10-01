"""门控执行。

门控把所有门动作串成一条链：开门前先核对对位是否落盘，关门后写落盘记录，发车
时清空指令队列。批量开门按门区里程排序并受批量上限约束，门区联动按站台门区映射
解析出实际门区。落盘事实只有一份：开门、关门、发车各一个文档。
"""

from __future__ import annotations

from .batch import batch_open
from .close import CloseRecord
from .close import close_and_audit, close_doors, close_durable
from .door import DoorController
from .group import active_group, open_group
from .limit import open_pressure
from .open import open_doors, opened
from .policy import depart_policy
from .queue import depart, departed, enqueue, process_queue, queued_total
from .report import door_report
from .state import door_state

__all__ = [
    "CloseRecord",
    "DoorController",
    "active_group",
    "batch_open",
    "close_and_audit",
    "close_doors",
    "close_durable",
    "depart",
    "depart_policy",
    "departed",
    "door_report",
    "door_state",
    "enqueue",
    "open_doors",
    "open_group",
    "open_pressure",
    "opened",
    "process_queue",
    "queued_total",
]
