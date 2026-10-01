"""门控状态汇总。"""

from __future__ import annotations

from .close import close_durable
from .door import DoorController
from .open import opened
from .queue import departed, process_queue


def door_state(controller: DoorController, train_id: str) -> dict:
    """汇总一列车的开门、关门与发车状态。"""

    return {
        "train_id": train_id,
        "opened": opened(controller, train_id),
        "closed": close_durable(controller, train_id),
        "departed": departed(controller, train_id),
        "queued": len(process_queue(controller, train_id)),
    }
