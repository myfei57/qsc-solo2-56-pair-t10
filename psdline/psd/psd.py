"""站台门登记与开闭。"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import RailError
from ..store import JsonStore


@dataclass
class PSD:
    """一樘站台门（或以门区为单位的一组门）。"""

    id: str
    station: str = ""
    group: str = ""
    open: bool = False
    locked: bool = False

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "station": self.station,
            "group": self.group,
            "open": self.open,
            "locked": self.locked,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "PSD":
        return cls(
            id=str(payload.get("id", "")),
            station=str(payload.get("station", "")),
            group=str(payload.get("group", "")),
            open=bool(payload.get("open", False)),
            locked=bool(payload.get("locked", False)),
        )


class PSDRegistry:
    """站台门登记表，落在 ``psd/<id>`` 文档上。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store

    @staticmethod
    def _doc_id(psd_id: str) -> str:
        return f"psd/{psd_id}"

    def upsert(self, gate: PSD) -> PSD:
        """写入或更新一樘站台门。"""

        if not gate.id:
            raise RailError("站台门标识为空", component="psd")
        self._store.write(self._doc_id(gate.id), gate.as_dict())
        return gate

    def get(self, psd_id: str) -> PSD | None:
        """读取一樘站台门，不存在时返回 ``None``。"""

        raw = self._store.read(self._doc_id(psd_id))
        if not isinstance(raw, dict):
            return None
        return PSD.from_dict(raw)

    def require(self, psd_id: str) -> PSD:
        """读取一樘站台门，不存在时抛出异常。"""

        gate = self.get(psd_id)
        if gate is None:
            raise RailError(f"站台门不存在: {psd_id}", component="psd")
        return gate

    def save(self, gate: PSD) -> PSD:
        """把内存中的站台门对象写回持久化。"""

        return self.upsert(gate)

    def set_open(self, psd_id: str, open_state: bool) -> PSD:
        """设置开闭状态。"""

        gate = self.require(psd_id)
        gate.open = open_state
        return self.save(gate)

    def list(self) -> list[PSD]:
        """按标识排序列出全部站台门。"""

        found: list[PSD] = []
        for doc_id in self._store.list_ids():
            if not doc_id.startswith("psd/") or doc_id.count("/") != 1:
                continue
            raw = self._store.read(doc_id)
            if isinstance(raw, dict):
                found.append(PSD.from_dict(raw))
        return sorted(found, key=lambda item: item.id)

    def by_station(self, station_id: str) -> list[PSD]:
        """列出某个站台的全部站台门。"""

        return [gate for gate in self.list() if gate.station == station_id]

    def count_open(self) -> int:
        return len([gate for gate in self.list() if gate.open])
