"""站台与门区。

站台保存门区清单与布局，布局给出门区里程，排班给出追踪间隔。开门顺序按里程由
近及远生成，最近门区用于提示司机停稳位置；站台登记与布局校验在写入前完成，避免
把非法记录写进持久化。
"""

from __future__ import annotations

from .layout import DoorZone, Layout, LayoutRegistry
from .schedule import Schedule, ScheduleRegistry
from .station import Station, StationRegistry
from .validate import validate_station
from .zones import nearest_zone, open_order, spread_m

__all__ = [
    "DoorZone",
    "Layout",
    "LayoutRegistry",
    "Schedule",
    "ScheduleRegistry",
    "Station",
    "StationRegistry",
    "nearest_zone",
    "open_order",
    "spread_m",
    "validate_station",
]
