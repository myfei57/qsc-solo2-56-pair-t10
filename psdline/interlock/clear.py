"""按条件解除锁定。

故障清除后并不一定立刻解锁：还要确认该门区已经回到关闭位、且没有新的探测异常。
这里把"可否解锁"的判断单独列出来，联锁清除与恢复流程共用同一套条件。
"""

from __future__ import annotations

from ..psd import PSDRegistry


def releasable(gates: PSDRegistry, psd_id: str, gap_clear: bool) -> tuple[bool, str]:
    """判断一个门区当前可否解除锁定，返回（是否可解、原因）。"""

    gate = gates.get(psd_id)
    if gate is None:
        return False, "gate_missing"
    if gate.open:
        return False, "gate_open"
    if not gap_clear:
        return False, "gap_blocked"
    return True, "ok"


def release_ready(gates: PSDRegistry, psd_ids: list[str], gap_clear: bool) -> dict:
    """批量判断一组门区是否都已具备解锁条件。"""

    blocked: dict[str, str] = {}
    for psd_id in psd_ids:
        allowed, reason = releasable(gates, psd_id, gap_clear)
        if not allowed:
            blocked[psd_id] = reason
    return {"ready": not blocked, "blocked": blocked, "checked": len(psd_ids)}
