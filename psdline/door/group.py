"""门区联动。

门区联动按站台门区映射把逻辑门区解析成物理门区，再整组开门。映射过期（修订号
落后于布局）时拒绝动作，避免按旧布局把门开错位置。
"""

from __future__ import annotations

from ..errors import MappingError
from ..platform import MappingRegistry
from .door import DoorController


def active_key(station_id: str) -> str:
    return f"door/{station_id}/active_group"


def open_group(
    controller: DoorController,
    mapping: MappingRegistry,
    station_id: str,
    group_key: str,
) -> dict:
    """按映射打开一个门区组。"""

    resolved = mapping.resolve(station_id, group_key)
    gate_ids = [gate.id for gate in controller.gates.by_station(station_id) if gate.group == resolved]
    if not gate_ids:
        raise MappingError(
            f"映射 {group_key} → {resolved} 在站台 {station_id} 找不到对应门区",
            component="door",
        )
    locked = [gate_id for gate_id in gate_ids if controller.lockouts.is_locked(gate_id)]
    if locked:
        raise MappingError(f"门区 {resolved} 被联锁锁定: {','.join(locked)}", component="door")
    for gate_id in gate_ids:
        controller.gates.set_open(gate_id, True)
    controller.store.write(
        active_key(station_id),
        {"station_id": station_id, "group_key": group_key, "group": resolved},
    )
    return {"station_id": station_id, "group_key": group_key, "group": resolved, "opened": gate_ids}


def active_group(controller: DoorController, station_id: str) -> str:
    """返回站台当前联动的门区；没有联动时返回空串。"""

    raw = controller.store.read(active_key(station_id))
    if isinstance(raw, dict):
        return str(raw.get("group", ""))
    return ""
