"""链路判定重放。

把落盘的采样流水重新折叠一遍，独立推导出「当时应该这么判」的判定序列，
再与流水里实际落盘的判定逐条比对。两者一致说明监控器、恢复与重放走的
是同一份判定逻辑；不一致会把分叉点原样列出，供事后追查。

这是审计链的最后一环：任何一次降级、退避或放行，都能用这份重放回答
「凭什么这么判」。
"""

from __future__ import annotations

from .monitor import Session
from .quality import LinkTuning


def _decision_key(decision: dict) -> tuple:
    """判定比对键：类别、迁移方向与判定时刻。"""

    return (
        str(decision.get("kind", "")),
        str(decision.get("from", "")),
        str(decision.get("to", "")),
        round(float(decision.get("at", 0.0)), 3),
    )


def replay_train(records: list[dict], train_id: str, tuning: LinkTuning) -> dict:
    """重放一列车的链路判定并与落盘判定比对。"""

    session = Session(train_id, tuning)
    derived: list[dict] = []
    journaled: list[dict] = []
    for record in records:
        if not isinstance(record, dict) or str(record.get("train_id", "")) != train_id:
            continue
        if record.get("type") == "sample":
            derived.extend(session.fold_record(record))
        elif record.get("type") == "decision":
            journaled.append(record)
    divergences: list[dict] = []
    for index, (mine, theirs) in enumerate(zip(derived, journaled)):
        if _decision_key(mine) != _decision_key(theirs):
            divergences.append({"index": index, "derived": mine, "journaled": theirs})
    if len(derived) != len(journaled):
        divergences.append(
            {
                "index": min(len(derived), len(journaled)),
                "derived_count": len(derived),
                "journaled_count": len(journaled),
            }
        )
    return {
        "train_id": train_id,
        "consistent": not divergences,
        "samples": session.samples_ok + session.samples_missed,
        "ignored": session.samples_ignored,
        "final_state": session.state,
        "decisions": derived,
        "journaled": journaled,
        "divergences": divergences,
    }
