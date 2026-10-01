"""发车放行。

发车前必须同时满足两个条件：间隙探测已确认，且关门记录已落盘。两个条件缺一
不放行，放行失败时返回具体是哪一条没满足，便于值班员判断该去复检探测还是去
复检门控。
"""

from __future__ import annotations

import json

from ..gap import GapRegistry
from ..store import JsonStore
from .sig import SignalController


def close_doc_id(train_id: str) -> str:
    return f"door/{train_id}/close"


def close_durable(store: JsonStore, train_id: str) -> bool:
    """读取关门记录并判断是否已落盘。"""

    raw = store.read(close_doc_id(train_id))
    if isinstance(raw, dict):
        return bool(raw.get("durable", False))
    if isinstance(raw, str):
        try:
            return bool(json.loads(raw).get("durable", False))
        except json.JSONDecodeError:
            return False
    return False


def depart_checked(
    signals: SignalController,
    store: JsonStore,
    gaps: GapRegistry,
    train_id: str,
    at: float,
) -> dict:
    """按顺序核对间隙确认与关门落盘，然后升起发车信号。"""

    if not gaps.clear(train_id):
        return {
            "ok": False,
            "reason": "gap_not_clear",
            "train_id": train_id,
            "violations": ["间隙探测未确认"],
        }
    if not close_durable(store, train_id):
        return {
            "ok": False,
            "reason": "close_not_durable",
            "train_id": train_id,
            "violations": ["关门未落盘"],
        }
    signals.raise_signal(train_id, "depart", at)
    return {"ok": True, "train_id": train_id, "aspect": "green", "violations": []}
