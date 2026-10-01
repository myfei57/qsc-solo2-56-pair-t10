"""整站清扫。

交接班前要把整站门区收回关闭位并逐条记录异常：某一樘门写失败时立刻停下并报出
是哪一樘，而不是把剩下的门刷完留下半开现场。
"""

from __future__ import annotations

from ..errors import RailError
from .psd import PSDRegistry


def sweep_station(gates: PSDRegistry, station_id: str) -> dict:
    """把某站台的全部站台门收回关闭位。"""

    targets = gates.by_station(station_id)
    closed: list[str] = []
    for gate in targets:
        if not gate.open:
            continue
        try:
            gates.set_open(gate.id, False)
        except RailError as error:
            raise RailError(
                f"清扫在门 {gate.id} 处失败: {error.message}", component="psd"
            ) from error
        closed.append(gate.id)
    return {
        "station_id": station_id,
        "closed": len(closed),
        "gates": closed,
        "scanned": len(targets),
    }
