"""审计统计。"""

from __future__ import annotations

from .entry import AuditEntry


def count(entries: list[AuditEntry]) -> int:
    """返回审计条数。"""

    return len(entries)


def last(entries: list[AuditEntry]) -> AuditEntry | None:
    """返回最后一条审计条目。"""

    return entries[-1] if entries else None


def latest_by_kind(entries: list[AuditEntry], kind: str) -> AuditEntry | None:
    """返回某动作类型最近的一条。"""

    for entry in reversed(entries):
        if entry.kind == kind:
            return entry
    return None
