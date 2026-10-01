"""站台门区布局。

布局给出门区里程，开门顺序与间隙探测都以里程为准。布局按站台持久化，重排后
里程可能整体平移，因此读取时返回的是布局快照而不是引用。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import RailError
from ..store import JsonStore


@dataclass(frozen=True)
class DoorZone:
    """一个门区及其里程。"""

    id: str
    distance_m: int
    side: str = "left"

    def as_dict(self) -> dict:
        return {"id": self.id, "distance_m": self.distance_m, "side": self.side}

    @classmethod
    def from_dict(cls, payload: dict) -> "DoorZone":
        return cls(
            id=str(payload.get("id", "")),
            distance_m=int(payload.get("distance_m", 0)),
            side=str(payload.get("side", "left")),
        )


@dataclass
class Layout:
    """一个站台的门区布局。"""

    station_id: str
    zones: list[DoorZone] = field(default_factory=list)
    revision: int = 1

    def as_dict(self) -> dict:
        return {
            "station_id": self.station_id,
            "revision": self.revision,
            "zones": [zone.as_dict() for zone in self.zones],
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Layout":
        zones = payload.get("zones", [])
        return cls(
            station_id=str(payload.get("station_id", "")),
            zones=[DoorZone.from_dict(item) for item in zones if isinstance(item, dict)],
            revision=int(payload.get("revision", 1)),
        )

    def zone(self, zone_id: str) -> DoorZone:
        """按标识取门区。"""

        for item in self.zones:
            if item.id == zone_id:
                return item
        raise RailError(f"门区不存在: {zone_id}", component="station")


class LayoutRegistry:
    """布局读写。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store

    @staticmethod
    def _doc_id(station_id: str) -> str:
        return f"station/{station_id}/layout"

    def save(self, layout: Layout) -> Layout:
        """保存一份布局，同一站台再次保存时递增修订号。"""

        if not layout.station_id:
            raise RailError("布局的站台标识为空", component="station")
        if not layout.zones:
            raise RailError("布局没有门区", component="station")
        current = self.get(layout.station_id)
        if current is not None:
            layout = Layout(
                station_id=layout.station_id,
                zones=list(layout.zones),
                revision=current.revision + 1,
            )
        self._store.write(self._doc_id(layout.station_id), layout.as_dict())
        return layout

    def get(self, station_id: str) -> Layout | None:
        """读取布局，不存在时返回 ``None``。"""

        raw = self._store.read(self._doc_id(station_id))
        if not isinstance(raw, dict):
            return None
        return Layout.from_dict(raw)

    def zone_distance(self, station_id: str, zone_id: str) -> int:
        """返回某门区的里程。"""

        layout = self.get(station_id)
        if layout is None:
            raise RailError(f"站台 {station_id} 没有布局", component="station")
        return layout.zone(zone_id).distance_m
