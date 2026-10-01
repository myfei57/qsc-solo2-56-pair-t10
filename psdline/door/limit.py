"""开门数量约束。

同步开门数量必须受限：一次放行太多门区，站台侧来不及确认，反而容易出现夹人。
这里给出当前同步开门数与上限的比较结果。
"""

from __future__ import annotations

from .door import DoorController


def open_pressure(controller: DoorController, station_id: str) -> dict:
    """返回一个站台的同步开门压力。"""

    gates = controller.gates.by_station(station_id)
    open_count = len([gate for gate in gates if gate.open])
    limit = controller.batch_limit()
    return {
        "station_id": station_id,
        "open_count": open_count,
        "limit": limit,
        "within_limit": open_count <= limit,
        "headroom": limit - open_count,
    }
