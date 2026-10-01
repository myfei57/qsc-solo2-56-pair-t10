"""运行交路。

交路记录"从哪站到哪站、走行距离多少"，用于站台排班与追踪间隔校验。写入前校验
起终点不同且距离为正。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import RailError
from ..store import JsonStore


@dataclass(frozen=True)
class Route:
    """一条运行交路。"""

    train_id: str
    origin: str
    destination: str
    distance_m: int

    def as_dict(self) -> dict:
        return {
            "train_id": self.train_id,
            "origin": self.origin,
            "destination": self.destination,
            "distance_m": self.distance_m,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Route":
        return cls(
            train_id=str(payload.get("train_id", "")),
            origin=str(payload.get("origin", "")),
            destination=str(payload.get("destination", "")),
            distance_m=int(payload.get("distance_m", 0)),
        )


class RouteRegistry:
    """交路读写。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store

    @staticmethod
    def _doc_id(train_id: str) -> str:
        return f"train/{train_id}/route"

    def assign(self, route: Route) -> Route:
        """写入一条交路。"""

        if not route.train_id:
            raise RailError("交路的列车标识为空", component="train")
        if not route.origin or not route.destination:
            raise RailError("交路起终点不能为空", component="train")
        if route.origin == route.destination:
            raise RailError("交路起终点不能相同", component="train")
        if route.distance_m <= 0:
            raise RailError("交路距离必须为正", component="train")
        self._store.write(self._doc_id(route.train_id), route.as_dict())
        return route

    def get(self, train_id: str) -> Route | None:
        """读取交路，不存在时返回 ``None``。"""

        raw = self._store.read(self._doc_id(train_id))
        if not isinstance(raw, dict):
            return None
        return Route.from_dict(raw)
