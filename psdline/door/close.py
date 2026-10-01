"""关门动作与落盘。

关门先写落盘记录，再由审计登记；审计写失败时必须把关门记录回滚，否则会出现
"门看起来关了、审计里没有"的不可解释现场。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..audit import AuditLog
from .door import DoorController


@dataclass(frozen=True)
class CloseRecord:
    """一次性关门记录。"""

    train_id: str
    durable: bool
    at: float

    def as_dict(self) -> dict:
        return {"train_id": self.train_id, "durable": self.durable, "at": self.at}


def close_doc_id(train_id: str) -> str:
    return f"door/{train_id}/close"


def close_doors(controller: DoorController, train_id: str, at: float) -> CloseRecord:
    """写关门落盘记录。"""

    record = CloseRecord(train_id=train_id, durable=True, at=at)
    controller.store.write(close_doc_id(train_id), record.as_dict())
    controller.note_close()
    return record


def close_and_audit(
    controller: DoorController,
    audit: AuditLog,
    train_id: str,
    station_id: str,
    kind: str,
    at: float,
) -> dict:
    """关门落盘后再写审计；审计失败时回滚关门记录。"""

    close_doors(controller, train_id, at)
    try:
        entry = audit.record(kind, train_id, station_id, at)
    except Exception:
        controller.store.remove(close_doc_id(train_id))
        raise
    return {
        "ok": True,
        "train_id": train_id,
        "close_durable": True,
        "audit_id": entry.id,
        "violations": [],
    }


def close_durable(controller: DoorController, train_id: str) -> bool:
    """判断关门记录是否已落盘。"""

    raw = controller.store.read(close_doc_id(train_id))
    return bool(isinstance(raw, dict) and raw.get("durable", False))
