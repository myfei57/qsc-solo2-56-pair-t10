"""联锁启用开关。

检修期间允许退出联锁，但退出状态必须可查、可追踪。启用标志按列车保存：置位写
文档，取消直接删除文档，查询即看文件在不在。
"""

from __future__ import annotations

from ..store import JsonStore


def arm_key(train_id: str) -> str:
    return f"interlock/{train_id}/arm"


class InterlockArmer:
    """按列车维护联锁启用状态。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store
        self._arms = 0
        self._disarms = 0

    @property
    def arms(self) -> int:
        return self._arms

    @property
    def disarms(self) -> int:
        return self._disarms

    def arm(self, train_id: str, armed: bool) -> bool:
        """启用或退出一列车的联锁，返回最终状态。"""

        if armed:
            self._store.write(arm_key(train_id), {"train_id": train_id, "state": "armed"})
            self._arms += 1
            return True
        self._store.remove(arm_key(train_id))
        self._disarms += 1
        return False

    def armed(self, train_id: str) -> bool:
        """查询一列车的联锁是否启用。"""

        return self._store.exists(arm_key(train_id))

    def armed_trains(self) -> list[str]:
        """返回当前已启用联锁的列车。"""

        found: list[str] = []
        for doc_id in self._store.list_ids():
            if not doc_id.startswith("interlock/") or not doc_id.endswith("/arm"):
                continue
            found.append(doc_id.split("/")[1])
        return sorted(found)
