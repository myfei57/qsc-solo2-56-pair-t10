"""门控指令队列。

批量开门与回绕处理要求"同一列车的门控指令按提交顺序执行、发车后清空"。指令
队列按 ``train_id`` 分文档存放，每条指令带 ``epoch``（提交时的代际），读取时
原样返回，供上位机核对有没有被重放。
"""

from __future__ import annotations

from dataclasses import dataclass

from .jsonfile import JsonStore


@dataclass(frozen=True)
class DoorCommand:
    """一条待执行的门控指令。"""

    id: str
    train_id: str
    kind: str
    epoch: int
    station_id: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "train_id": self.train_id,
            "kind": self.kind,
            "epoch": self.epoch,
            "station_id": self.station_id,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "DoorCommand":
        return cls(
            id=str(payload.get("id", "")),
            train_id=str(payload.get("train_id", "")),
            kind=str(payload.get("kind", "")),
            epoch=int(payload.get("epoch", 0)),
            station_id=str(payload.get("station_id", "")),
        )


class CommandLog:
    """按列车保存的门控指令队列。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store
        self._epoch = 0

    @property
    def epoch(self) -> int:
        """返回当前代际，每次清空队列后递增。"""

        return self._epoch

    def _doc_id(self, train_id: str) -> str:
        return f"commands/{train_id}"

    def append(self, command: DoorCommand) -> None:
        """把一条指令追加到列车队列尾部。"""

        queued = self.pending(command.train_id)
        queued.append(command)
        self._store.write(self._doc_id(command.train_id), [item.as_dict() for item in queued])

    def pending(self, train_id: str) -> list[DoorCommand]:
        """返回列车当前排队中的指令（按提交顺序）。"""

        raw = self._store.read(self._doc_id(train_id))
        if not isinstance(raw, list):
            return []
        return [DoorCommand.from_dict(item) for item in raw if isinstance(item, dict)]

    def ids(self, train_id: str) -> list[str]:
        """返回排队指令的标识列表。"""

        return [command.id for command in self.pending(train_id)]

    def clear(self, train_id: str) -> int:
        """清空列车队列并推进代际，返回被清掉的指令条数。"""

        removed = len(self.pending(train_id))
        self._store.remove(self._doc_id(train_id))
        self._epoch += 1
        return removed

    def total_queued(self) -> int:
        """统计全部列车排队中的指令条数。"""

        total = 0
        for doc_id in self._store.list_ids():
            if not doc_id.startswith("commands/"):
                continue
            raw = self._store.read(doc_id)
            if isinstance(raw, list):
                total += len(raw)
        return total
