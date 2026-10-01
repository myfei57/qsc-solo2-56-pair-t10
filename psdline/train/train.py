"""列车登记与查询。"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import RailError
from ..store import JsonStore


@dataclass
class Train:
    """一列在线运行的车。"""

    id: str
    line: str = ""
    station: str = ""
    position_m: int = 0

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "line": self.line,
            "station": self.station,
            "position_m": self.position_m,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Train":
        return cls(
            id=str(payload.get("id", "")),
            line=str(payload.get("line", "")),
            station=str(payload.get("station", "")),
            position_m=int(payload.get("position_m", 0)),
        )


class TrainRegistry:
    """列车登记表，落在 ``train/<id>`` 文档上。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store

    @staticmethod
    def _doc_id(train_id: str) -> str:
        return f"train/{train_id}"

    def upsert(self, train: Train) -> Train:
        """写入或更新一列车。"""

        if not train.id:
            raise RailError("列车标识为空", component="train")
        self._store.write(self._doc_id(train.id), train.as_dict())
        return train

    def get(self, train_id: str) -> Train | None:
        """读取一列车，不存在时返回 ``None``。"""

        raw = self._store.read(self._doc_id(train_id))
        if not isinstance(raw, dict):
            return None
        return Train.from_dict(raw)

    def require(self, train_id: str) -> Train:
        """读取一列车，不存在时抛出异常。"""

        train = self.get(train_id)
        if train is None:
            raise RailError(f"列车不存在: {train_id}", component="train")
        return train

    def assign_station(self, train_id: str, station_id: str) -> Train:
        """把列车挂到站台。"""

        train = self.require(train_id)
        train.station = station_id
        return self.upsert(train)

    def list(self) -> list[Train]:
        """按标识排序列出全部列车。"""

        found: list[Train] = []
        for doc_id in self._store.list_ids():
            if not doc_id.startswith("train/") or doc_id.count("/") != 1:
                continue
            raw = self._store.read(doc_id)
            if isinstance(raw, dict):
                found.append(Train.from_dict(raw))
        return sorted(found, key=lambda item: item.id)

    def count(self) -> int:
        return len(self.list())
