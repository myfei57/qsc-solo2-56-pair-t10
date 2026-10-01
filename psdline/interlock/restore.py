"""重启后的锁定状态还原。

控制服务重启后发现场门与故障登记表可能不一致，因此恢复流程以故障登记表为准：
仍置位的门重新锁上，已清除的门解锁。恢复过程逐门返回动作，便于巡检核对。
"""

from __future__ import annotations

from ..psd import PSDRegistry
from .clear import releasable
from .interlock import ACTIVE, FaultRegistry


def restore_lockouts(
    faults: FaultRegistry,
    gates: PSDRegistry,
    lockouts,
    gap_clear: bool,
) -> dict:
    """按故障登记表还原全部门的锁定状态。"""

    actions: list[dict] = []
    for gate in gates.list():
        state = faults.state(gate.id)
        if state == ACTIVE:
            lockouts.set(gate.id, True)
            actions.append({"psd_id": gate.id, "action": "locked", "reason": "fault_active"})
            continue
        allowed, reason = releasable(gates, gate.id, gap_clear)
        if gate.locked and not allowed:
            actions.append({"psd_id": gate.id, "action": "kept_locked", "reason": reason})
            continue
        lockouts.clear(gate.id)
        actions.append({"psd_id": gate.id, "action": "released", "reason": "fault_clear"})
    return {
        "restored": len(actions),
        "locked": len([item for item in actions if item["action"] == "locked"]),
        "released": len([item for item in actions if item["action"] == "released"]),
        "actions": actions,
    }
