"""站台门锁定。

联锁把门锁在关闭位（例如间隙探测异常、故障未清除），但解除锁定的条件由联锁
自己判定，门控侧只提供"设置/清除/查询"三个入口。锁定标志与开闭标志分开保存，
这样一次批量开门不会顺手把锁也解掉。
"""

from __future__ import annotations

from .psd import PSD, PSDRegistry


class LockoutRegistry:
    """锁定状态读写。"""

    def __init__(self, gates: PSDRegistry) -> None:
        self._gates = gates
        self._changes = 0

    @property
    def changes(self) -> int:
        """返回锁定状态变更次数。"""

        return self._changes

    def set(self, psd_id: str, locked: bool) -> PSD:
        """把一樘门设为锁定或解锁。"""

        gate = self._gates.require(psd_id)
        gate.locked = locked
        self._changes += 1
        return self._gates.save(gate)

    def clear(self, psd_id: str) -> PSD:
        """解除一樘门的锁定。"""

        return self.set(psd_id, False)

    def is_locked(self, psd_id: str) -> bool:
        """查询一樘门是否被锁定。"""

        return self._gates.require(psd_id).locked

    def locked_ids(self) -> list[str]:
        """返回当前处于锁定状态的门标识（排序）。"""

        return sorted(gate.id for gate in self._gates.list() if gate.locked)

    def locked_count(self) -> int:
        return len(self.locked_ids())

    def locked_ratio(self) -> float:
        """返回锁定比例，供联锁判定是否可以继续放行。"""

        gates = self._gates.list()
        if not gates:
            return 0.0
        return len([gate for gate in gates if gate.locked]) / len(gates)
