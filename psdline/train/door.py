"""列车侧车门确认状态。

站台门与车门是两套设备：站台门由门控动作打开，车门状态由车载侧确认。控制链
要求"对位落盘 → 开站台门 → 确认车门打开"，因此这里单独保存一份车门状态，
发车检查会同时读它和站台门的落盘状态。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..store import JsonStore


@dataclass(frozen=True)
class DoorState:
    """车载侧车门确认状态。"""

    train_id: str
    open: bool
    confirmed_at: float

    def as_dict(self) -> dict:
        return {"train_id": self.train_id, "open": self.open, "confirmed_at": self.confirmed_at}

    @classmethod
    def from_dict(cls, payload: dict) -> "DoorState":
        return cls(
            train_id=str(payload.get("train_id", "")),
            open=bool(payload.get("open", False)),
            confirmed_at=float(payload.get("confirmed_at", 0.0)),
        )


class DoorStateRegistry:
    """车门确认状态读写。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store

    @staticmethod
    def _doc_id(train_id: str) -> str:
        return f"train/{train_id}/door"

    def confirm_open(self, train_id: str, at: float) -> DoorState:
        """登记"车门已确认打开"。"""

        state = DoorState(train_id=train_id, open=True, confirmed_at=at)
        self._store.write(self._doc_id(train_id), state.as_dict())
        return state

    def get(self, train_id: str) -> DoorState | None:
        """读取车门状态，不存在时返回 ``None``。"""

        raw = self._store.read(self._doc_id(train_id))
        if not isinstance(raw, dict):
            return None
        return DoorState.from_dict(raw)

    def is_open(self, train_id: str) -> bool:
        """判断车门是否处于确认打开状态。"""

        state = self.get(train_id)
        return bool(state and state.open)
