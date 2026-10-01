"""联锁状态快照。"""

from __future__ import annotations

from ..psd import PSDRegistry
from .interlock import FaultRegistry


def interlock_status(
    faults: FaultRegistry,
    gates: PSDRegistry,
    max_locked_ratio: float = 0.5,
) -> dict:
    """汇总故障、锁定与放行余量。"""

    active = faults.active_ids()
    locked = [gate.id for gate in gates.list() if gate.locked]
    total = len(gates.list())
    ratio = len(locked) / total if total else 0.0
    return {
        "faults_active": active,
        "fault_count": len(active),
        "locked": locked,
        "locked_count": len(locked),
        "locked_ratio": round(ratio, 4),
        "max_locked_ratio": max_locked_ratio,
        "can_release_depart": ratio <= max_locked_ratio,
    }
