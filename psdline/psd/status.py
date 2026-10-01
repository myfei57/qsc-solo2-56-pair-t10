"""站台门状态快照。"""

from __future__ import annotations

from .psd import PSDRegistry


def gate_status(gates: PSDRegistry, psd_id: str) -> dict:
    """返回一樘门的状态快照。"""

    gate = gates.get(psd_id)
    if gate is None:
        return {"id": psd_id, "found": False}
    return {
        "id": gate.id,
        "found": True,
        "station": gate.station,
        "group": gate.group,
        "open": gate.open,
        "locked": gate.locked,
    }


def station_status(gates: PSDRegistry, station_id: str) -> dict:
    """返回一个站台的门区状态汇总。"""

    items = gates.by_station(station_id)
    return {
        "station_id": station_id,
        "count": len(items),
        "open": len([gate for gate in items if gate.open]),
        "locked": len([gate for gate in items if gate.locked]),
        "groups": sorted({gate.group for gate in items}),
    }
