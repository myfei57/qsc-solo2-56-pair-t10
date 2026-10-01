"""停稳对位记录。

对位记录是"停稳"的唯一凭据：动作链要求开门前对位必须已落盘（``durable`` 为真）。
对位位置超出公差时直接拒绝并保留上一次的有效记录，避免用半个位置去开门。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import AlignmentError
from ..store import JsonStore


@dataclass(frozen=True)
class Alignment:
    """一次停稳对位。"""

    train_id: str
    position_m: int
    durable: bool
    at: float

    def as_dict(self) -> dict:
        return {
            "train_id": self.train_id,
            "position_m": self.position_m,
            "durable": self.durable,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Alignment":
        return cls(
            train_id=str(payload.get("train_id", "")),
            position_m=int(payload.get("position_m", 0)),
            durable=bool(payload.get("durable", False)),
            at=float(payload.get("at", 0.0)),
        )


class AlignRegistry:
    """对位记录读写。"""

    def __init__(self, store: JsonStore, tolerance_m: float = 0.8) -> None:
        self._store = store
        self._tolerance_m = tolerance_m

    @property
    def tolerance_m(self) -> float:
        return self._tolerance_m

    @staticmethod
    def _doc_id(train_id: str) -> str:
        return f"train/{train_id}/align"

    def align(self, train_id: str, position_m: int, stop_position_m: int, at: float) -> Alignment:
        """登记一次对位；偏差超公差时拒绝，不覆盖上一次有效记录。"""

        if not train_id:
            raise AlignmentError("对位的列车标识为空", component="train")
        offset = abs(position_m - stop_position_m)
        if offset > self._tolerance_m:
            raise AlignmentError(
                f"对位偏差 {offset:.3f} m 超出公差 {self._tolerance_m:.3f} m",
                component="train",
            )
        record = Alignment(
            train_id=train_id, position_m=position_m, durable=True, at=at
        )
        self._store.write(self._doc_id(train_id), record.as_dict())
        return record

    def get(self, train_id: str) -> Alignment | None:
        """读取对位记录，不存在时返回 ``None``。"""

        raw = self._store.read(self._doc_id(train_id))
        if not isinstance(raw, dict):
            return None
        return Alignment.from_dict(raw)

    def durable(self, train_id: str) -> bool:
        """判断对位是否已落盘。"""

        record = self.get(train_id)
        return bool(record and record.durable)

    def clear(self, train_id: str) -> bool:
        """清掉对位记录，发车后调用。"""

        return self._store.remove(self._doc_id(train_id))
