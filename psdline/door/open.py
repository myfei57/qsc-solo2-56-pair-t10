"""开门动作。

开门是"对位落盘之后"的第一个动作：读不到对位记录、或对位记录没落盘，都直接
拒绝。开门事实写成 ``door/<train>/open`` 文档，发车前由开门状态推导。
"""

from __future__ import annotations

from ..errors import InterlockError
from .door import DoorController


def open_doc_id(train_id: str) -> str:
    return f"door/{train_id}/open"


def open_doors(controller: DoorController, train_id: str, at: float) -> dict:
    """按对位记录打开一列车的站台门。"""

    alignment = controller.align.get(train_id)
    if alignment is None:
        raise InterlockError("列车没有对位记录", component="door")
    if not alignment.durable:
        raise InterlockError("列车对位尚未落盘", component="door")
    controller.store.write(
        open_doc_id(train_id),
        {"train_id": train_id, "position_m": alignment.position_m, "at": at},
    )
    controller.note_open()
    return {
        "ok": True,
        "train_id": train_id,
        "position_m": alignment.position_m,
        "violations": [],
    }


def opened(controller: DoorController, train_id: str) -> bool:
    """判断一列车的站台门是否已打开。"""

    return controller.store.exists(open_doc_id(train_id))
