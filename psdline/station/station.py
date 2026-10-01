"""站台登记与查询。"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import RailError
from ..store import JsonStore
from .validate import validate_station


@dataclass
class Station:
    """一个站台。"""

    id: str
    name: str = ""
    line: str = ""
    door_groups: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "line": self.line,
            "door_groups": list(self.door_groups),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Station":
        groups = payload.get("door_groups", [])
        return cls(
            id=str(payload.get("id", "")),
            name=str(payload.get("name", "")),
            line=str(payload.get("line", "")),
            door_groups=[str(item) for item in groups] if isinstance(groups, list) else [],
        )


class StationRegistry:
    """站台登记表，落在 ``station/<id>`` 文档上。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store

    @staticmethod
    def _doc_id(station_id: str) -> str:
        return f"station/{station_id}"

    def upsert(self, station: Station) -> Station:
        """写入或更新一个站台，写入前先做完整校验。"""

        validate_station(station)
        self._store.write(self._doc_id(station.id), station.as_dict())
        return station

    def get(self, station_id: str) -> Station | None:
        """读取一个站台，不存在时返回 ``None``。"""

        raw = self._store.read(self._doc_id(station_id))
        if not isinstance(raw, dict):
            return None
        return Station.from_dict(raw)

    def require(self, station_id: str) -> Station:
        """读取一个站台，不存在时抛出异常。"""

        station = self.get(station_id)
        if station is None:
            raise RailError(f"站台不存在: {station_id}", component="station")
        return station

    def list(self) -> list[Station]:
        """按站台标识排序列出全部站台。"""

        found: list[Station] = []
        for doc_id in self._store.list_ids():
            if not doc_id.startswith("station/") or doc_id.count("/") != 1:
                continue
            raw = self._store.read(doc_id)
            if isinstance(raw, dict):
                found.append(Station.from_dict(raw))
        return sorted(found, key=lambda item: item.id)

    def group_count(self, station_id: str) -> int:
        """返回站台的门区数量。"""

        station = self.get(station_id)
        return len(station.door_groups) if station else 0
