"""信号状态快照。"""

from __future__ import annotations

from .aspect import aspect_of
from .sig import SignalController


def signal_status(signals: SignalController, train_id: str) -> dict:
    """返回一列车的灯位状态。"""

    return {
        "train_id": train_id,
        "arrival": signals.is_raised(train_id, "arrival"),
        "depart": signals.is_raised(train_id, "depart"),
        "aspect": aspect_of(signals, train_id),
    }


def signal_summary(signals: SignalController, train_ids: list[str]) -> dict:
    """汇总多列车的灯位状态。"""

    items = [signal_status(signals, train_id) for train_id in train_ids]
    return {
        "count": len(items),
        "green": len([item for item in items if item["aspect"] == "green"]),
        "yellow": len([item for item in items if item["aspect"] == "yellow"]),
        "red": len([item for item in items if item["aspect"] == "red"]),
        "trains": items,
    }
