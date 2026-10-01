"""按资产重建审计时间线。"""

from __future__ import annotations

from .entry import AuditEntry


def build_timeline(entries: list[AuditEntry], train_id: str = "", limit: int = 50) -> list[dict]:
    """把审计条目按时间排序成一条时间线。"""

    selected = [entry for entry in entries if not train_id or entry.train_id == train_id]
    selected.sort(key=lambda item: item.at)
    steps: list[dict] = []
    for index, entry in enumerate(selected[-limit:], start=1):
        steps.append(
            {
                "step": index,
                "id": entry.id,
                "kind": entry.kind,
                "train_id": entry.train_id,
                "station_id": entry.station_id,
                "at": entry.at,
                "detail": dict(entry.detail),
            }
        )
    return steps
