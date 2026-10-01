"""间隙状态快照。"""

from __future__ import annotations

from .gap import GapRegistry


def gap_status(gaps: GapRegistry, train_id: str) -> dict:
    """返回一列车的间隙状态。"""

    return {
        "train_id": train_id,
        "state": gaps.state(train_id),
        "clear": gaps.clear(train_id),
    }


def gap_summary(gaps: GapRegistry, train_ids: list[str]) -> dict:
    """汇总多列车的间隙状态。"""

    items = [gap_status(gaps, train_id) for train_id in train_ids]
    return {
        "count": len(items),
        "clear": len([item for item in items if item["clear"]]),
        "blocked": gaps.blocked_ids(),
        "trains": items,
    }
