"""停站时长。

停站时长由车载侧给出，用于发车前的放行窗口。为了不把异常值写进持久化，写入前
校验"列车存在、时长非负且不超过上限"。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import RailError
from ..store import JsonStore


@dataclass(frozen=True)
class Dwell:
    """一次停站时长。"""

    train_id: str
    seconds: float
    station_id: str = ""

    def as_dict(self) -> dict:
        return {"train_id": self.train_id, "seconds": self.seconds, "station_id": self.station_id}

    @classmethod
    def from_dict(cls, payload: dict) -> "Dwell":
        return cls(
            train_id=str(payload.get("train_id", "")),
            seconds=float(payload.get("seconds", 0.0)),
            station_id=str(payload.get("station_id", "")),
        )


class DwellRegistry:
    """停站时长读写。"""

    def __init__(self, store: JsonStore, max_seconds: float = 600.0) -> None:
        self._store = store
        self._max_seconds = max_seconds

    @property
    def max_seconds(self) -> float:
        return self._max_seconds

    @staticmethod
    def _doc_id(train_id: str) -> str:
        return f"train/{train_id}/dwell"

    def set(self, dwell: Dwell) -> Dwell:
        """写入停站时长。"""

        if not dwell.train_id:
            raise RailError("停站时长的列车标识为空", component="train")
        if dwell.seconds < 0:
            raise RailError("停站时长不能为负", component="train")
        if dwell.seconds > self._max_seconds:
            raise RailError(
                f"停站时长 {dwell.seconds:.1f}s 超过上限 {self._max_seconds:.1f}s",
                component="train",
            )
        self._store.write(self._doc_id(dwell.train_id), dwell.as_dict())
        return dwell

    def get(self, train_id: str) -> Dwell | None:
        """读取停站时长，不存在时返回 ``None``。"""

        raw = self._store.read(self._doc_id(train_id))
        if not isinstance(raw, dict):
            return None
        return Dwell.from_dict(raw)

    def remaining(self, train_id: str, elapsed_s: float) -> float:
        """返回剩余的停站时间；没有登记时返回 0。"""

        dwell = self.get(train_id)
        if dwell is None:
            return 0.0
        return max(0.0, dwell.seconds - elapsed_s)
