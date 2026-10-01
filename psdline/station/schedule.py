"""站台排班与追踪间隔。"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import RailError
from ..store import JsonStore


@dataclass
class Schedule:
    """一个站台的排班（追踪间隔序列，单位秒）。"""

    station_id: str
    headways: list[int] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"station_id": self.station_id, "headways": list(self.headways)}

    @classmethod
    def from_dict(cls, payload: dict) -> "Schedule":
        headways = payload.get("headways", [])
        return cls(
            station_id=str(payload.get("station_id", "")),
            headways=[int(item) for item in headways] if isinstance(headways, list) else [],
        )


class ScheduleRegistry:
    """排班读写。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store

    @staticmethod
    def _doc_id(station_id: str) -> str:
        return f"station/{station_id}/schedule"

    def save(self, schedule: Schedule) -> Schedule:
        """保存排班。"""

        if not schedule.station_id:
            raise RailError("排班的站台标识为空", component="station")
        if not schedule.headways:
            raise RailError("排班没有追踪间隔", component="station")
        if any(item <= 0 for item in schedule.headways):
            raise RailError("追踪间隔必须为正", component="station")
        self._store.write(self._doc_id(schedule.station_id), schedule.as_dict())
        return schedule

    def get(self, station_id: str) -> Schedule | None:
        """读取排班，不存在时返回 ``None``。"""

        raw = self._store.read(self._doc_id(station_id))
        if not isinstance(raw, dict):
            return None
        return Schedule.from_dict(raw)

    def minimum_headway(self, station_id: str) -> int:
        """返回最短追踪间隔。"""

        schedule = self.get(station_id)
        if schedule is None:
            raise RailError(f"站台 {station_id} 没有排班", component="station")
        return min(schedule.headways)
