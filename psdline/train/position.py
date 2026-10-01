"""列车位置刷新。

位置由信号侧刷新，刷新周期短、写入频繁，因此单独存一份纯数字文档（而不是整个
列车对象），避免高频刷新把站台归属一起重写。刷新时做上界校验与陈旧判定。
"""

from __future__ import annotations

from ..store import JsonStore


class PositionTracker:
    """列车位置跟踪。"""

    def __init__(self, store: JsonStore, max_position_m: int = 240, stale_after_s: float = 8.0) -> None:
        self._store = store
        self._max_position_m = max_position_m
        self._stale_after_s = stale_after_s

    @property
    def max_position_m(self) -> int:
        return self._max_position_m

    @staticmethod
    def _doc_id(train_id: str) -> str:
        return f"train/{train_id}/position"

    def update(self, train_id: str, position_m: int, at: float) -> dict:
        """刷新一列车的位置。"""

        if not train_id:
            return {"accepted": False, "reason": "missing_train"}
        if position_m < 0 or position_m > self._max_position_m:
            return {
                "accepted": False,
                "reason": "out_of_range",
                "position_m": position_m,
                "limit_m": self._max_position_m,
            }
        payload = {"train_id": train_id, "position_m": position_m, "at": at}
        self._store.write(self._doc_id(train_id), payload)
        return {"accepted": True, "position_m": position_m, "at": at}

    def get(self, train_id: str) -> int:
        """返回最近一次刷新的位置；没有记录时返回 0。"""

        raw = self._store.read(self._doc_id(train_id))
        if not isinstance(raw, dict):
            return 0
        return int(raw.get("position_m", 0))

    def age_s(self, train_id: str, now: float) -> float:
        """返回位置记录距现在的时间；没有记录时返回一个很大的值。"""

        raw = self._store.read(self._doc_id(train_id))
        if not isinstance(raw, dict):
            return float("inf")
        return max(0.0, now - float(raw.get("at", 0.0)))

    def stale(self, train_id: str, now: float) -> bool:
        """判断位置是否已经过期。"""

        return self.age_s(train_id, now) > self._stale_after_s
