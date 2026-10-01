"""批量开门。

批量开门按门区里程由近及远执行，并受批量上限约束：超限时只开最近的那一批，剩下
的留给下一个周期。被联锁锁定的门区直接跳过并记进 ``skipped``，不会静默漏开。
"""

from __future__ import annotations

from ..station import DoorZone, open_order
from .door import DoorController


def batch_open(
    controller: DoorController,
    station_id: str,
    zones: list[DoorZone],
) -> dict:
    """按里程顺序批量开门。"""

    order = open_order(zones)
    limit = controller.batch_limit()
    scheduled = order[:limit]
    deferred = order[limit:]
    opened: list[str] = []
    skipped: dict[str, str] = {}
    for group in scheduled:
        gate_ids = [gate.id for gate in controller.gates.by_station(station_id) if gate.group == group]
        if not gate_ids:
            skipped[group] = "gate_missing"
            continue
        if any(controller.lockouts.is_locked(gate_id) for gate_id in gate_ids):
            skipped[group] = "locked"
            continue
        for gate_id in gate_ids:
            controller.gates.set_open(gate_id, True)
        opened.append(group)
    return {
        "station_id": station_id,
        "order": order,
        "opened": opened,
        "deferred": deferred,
        "skipped": skipped,
        "limit": limit,
    }
