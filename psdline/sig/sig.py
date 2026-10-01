"""信号灯位控制。"""

from __future__ import annotations

from ..store import JsonStore


class SignalController:
    """灯位与位置刷新控制器。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store
        self._raises = 0
        self._clears = 0

    @property
    def raises(self) -> int:
        """返回累计升灯次数。"""

        return self._raises

    @property
    def clears(self) -> int:
        """返回累计熄灯次数。"""

        return self._clears

    @staticmethod
    def _doc_id(train_id: str, kind: str) -> str:
        return f"signal/{train_id}/{kind}"

    def raise_signal(self, train_id: str, kind: str, at: float) -> str:
        """升起一个灯位。"""

        if not train_id or not kind:
            raise ValueError("灯位标识不能为空")
        self._store.write(self._doc_id(train_id, kind), {"train_id": train_id, "kind": kind, "at": at})
        self._raises += 1
        return f"{train_id}:{kind}"

    def is_raised(self, train_id: str, kind: str) -> bool:
        """查询灯位是否已升起。"""

        return self._store.exists(self._doc_id(train_id, kind))

    def clear(self, train_id: str, kind: str) -> bool:
        """熄掉一个灯位。"""

        removed = self._store.remove(self._doc_id(train_id, kind))
        if removed:
            self._clears += 1
        return removed

    def raised_kinds(self, train_id: str) -> list[str]:
        """列出某列车当前升起的灯位。"""

        prefix = f"signal/{train_id}/"
        return sorted(
            doc_id[len(prefix) :] for doc_id in self._store.list_ids() if doc_id.startswith(prefix)
        )
