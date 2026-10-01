"""门控放行策略。

发车前的门控自检只回答"这列车现在能不能走"：开门记录、关门落盘、队列清空三件事
都要成立。策略是纯查询，不改任何状态，便于控制台与联锁各自调用。
"""

from __future__ import annotations

from .close import close_durable
from .door import DoorController
from .open import opened
from .queue import process_queue


def depart_policy(controller: DoorController, train_id: str) -> dict:
    """返回发车前的门控自检结果。"""

    checks = {
        "opened": opened(controller, train_id),
        "close_durable": close_durable(controller, train_id),
        "queue_empty": not process_queue(controller, train_id),
    }
    failures = [name for name, passed in checks.items() if not passed]
    return {"allowed": not failures, "checks": checks, "failures": failures}
