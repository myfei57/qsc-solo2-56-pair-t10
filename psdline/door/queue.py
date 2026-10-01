"""门控指令队列与发车。

同一列车的门控指令按提交顺序排队；发车时先把"已发车"落盘，再清空队列。先落盘
再清空保证：即使清空过程中掉电，重启后也能看到该车已经发过车，不会重放指令。
"""

from __future__ import annotations

from ..store import DoorCommand
from .door import DoorController


def departed_doc_id(train_id: str) -> str:
    return f"door/{train_id}/departed"


def enqueue(
    controller: DoorController,
    train_id: str,
    command_id: str,
    kind: str,
    station_id: str,
) -> DoorCommand:
    """把一条门控指令排进列车队列。"""

    command = DoorCommand(
        id=command_id,
        train_id=train_id,
        kind=kind,
        epoch=controller.commands.epoch,
        station_id=station_id,
    )
    controller.commands.append(command)
    return command


def process_queue(controller: DoorController, train_id: str) -> list[str]:
    """返回列车当前排队中的指令标识；已发车时返回空队列。"""

    if departed(controller, train_id):
        return []
    return controller.commands.ids(train_id)


def depart(controller: DoorController, train_id: str, at: float) -> dict:
    """把发车事实落盘并清空指令队列。"""

    controller.store.write(departed_doc_id(train_id), {"train_id": train_id, "at": at})
    cleared = controller.commands.clear(train_id)
    return {"train_id": train_id, "departed": True, "cleared_commands": cleared}


def departed(controller: DoorController, train_id: str) -> bool:
    """判断列车是否已发车。"""

    return controller.store.exists(departed_doc_id(train_id))


def queued_total(controller: DoorController) -> int:
    """返回全部门控队列里的待执行指令条数。"""

    return controller.commands.total_queued()
