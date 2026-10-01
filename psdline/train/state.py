"""列车状态汇总。"""

from __future__ import annotations

from .align import AlignRegistry
from .door import DoorStateRegistry
from .position import PositionTracker


def train_state(
    train_id: str,
    align: AlignRegistry,
    doors: DoorStateRegistry,
    positions: PositionTracker,
) -> dict:
    """汇总一列车的对位、车门与位置状态。"""

    alignment = align.get(train_id)
    door_open = doors.is_open(train_id)
    position_m = positions.get(train_id)
    return {
        "train_id": train_id,
        "aligned": bool(alignment and alignment.durable),
        "alignment_m": alignment.position_m if alignment else 0,
        "doors_open": door_open,
        "position_m": position_m,
    }
