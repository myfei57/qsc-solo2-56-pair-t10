"""联锁故障登记。

故障登记表记录每个门区"最近一次故障是置位还是已清除"，是锁定状态的唯一驱动
来源：置位时把门锁上，清除时按条件解锁。恢复流程（``restore``）靠这张表把
重启后的锁定状态还原。
"""

from __future__ import annotations

from ..errors import RailError
from ..psd import LockoutRegistry
from ..store import JsonStore

ACTIVE = "active"
CLEAR = "clear"


class FaultRegistry:
    """故障登记表。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store
        self._sets = 0
        self._clears = 0

    @property
    def sets(self) -> int:
        """返回累计置位次数。"""

        return self._sets

    @property
    def clears(self) -> int:
        """返回累计清除次数。"""

        return self._clears

    @staticmethod
    def _doc_id(psd_id: str) -> str:
        return f"interlock/{psd_id}/fault"

    def state(self, psd_id: str) -> str:
        """返回门区最近一次故障状态（``active`` / ``clear`` / 空串）。"""

        raw = self._store.read(self._doc_id(psd_id))
        if isinstance(raw, dict):
            return str(raw.get("state", ""))
        return ""

    def set_fault(self, lockouts: LockoutRegistry, psd_id: str, at: float) -> None:
        """置位故障并把对应门锁定。"""

        self._store.write(
            self._doc_id(psd_id), {"psd_id": psd_id, "state": ACTIVE, "at": at}
        )
        lockouts.set(psd_id, True)
        self._sets += 1

    def clear_fault(self, lockouts: LockoutRegistry, psd_id: str, at: float) -> None:
        """清除故障并按条件解锁。"""

        if self.state(psd_id) == "":
            raise RailError(f"门区没有故障记录: {psd_id}", component="interlock")
        self._store.write(
            self._doc_id(psd_id), {"psd_id": psd_id, "state": CLEAR, "at": at}
        )
        lockouts.clear(psd_id)
        self._clears += 1

    def active_ids(self) -> list[str]:
        """返回当前仍处于置位状态的门区标识。"""

        found: list[str] = []
        for doc_id in self._store.list_ids():
            if not doc_id.startswith("interlock/") or not doc_id.endswith("/fault"):
                continue
            raw = self._store.read(doc_id)
            if isinstance(raw, dict) and raw.get("state") == ACTIVE:
                found.append(str(raw.get("psd_id", "")))
        return sorted(found)
