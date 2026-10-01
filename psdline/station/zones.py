"""门区顺序与最近门区。

批量开门必须按里程由近及远执行：先开司机侧最近的门区，避免乘客从远端先下、在
站台上逆行。最近门区同时用于提示停稳位置，因此这两个函数是纯函数，便于在验证
环境里直接断言顺序。
"""

from __future__ import annotations

from .layout import DoorZone


def open_order(zones: list[DoorZone]) -> list[str]:
    """按里程由近及远返回门区标识；里程相同时保持输入顺序。"""

    ordered = sorted(zones, key=lambda zone: zone.distance_m)
    return [zone.id for zone in ordered]


def nearest_zone(zones: list[DoorZone]) -> DoorZone | None:
    """返回最近的门区，无门区时返回 ``None``。"""

    if not zones:
        return None
    return min(zones, key=lambda zone: zone.distance_m)


def spread_m(zones: list[DoorZone]) -> int:
    """返回门区里程跨度，用于校验布局是否被整体搬移。"""

    if not zones:
        return 0
    return max(zone.distance_m for zone in zones) - min(zone.distance_m for zone in zones)
