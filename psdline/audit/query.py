"""审计检索。"""

from __future__ import annotations

from .entry import AuditEntry


def by_kind(entries: list[AuditEntry], kind: str) -> list[AuditEntry]:
    """按动作类型过滤。"""

    return [entry for entry in entries if entry.kind == kind]


def by_train(entries: list[AuditEntry], train_id: str) -> list[AuditEntry]:
    """按列车过滤。"""

    return [entry for entry in entries if entry.train_id == train_id]


def kinds(entries: list[AuditEntry]) -> list[str]:
    """返回出现过的动作类型（排序）。"""

    return sorted({entry.kind for entry in entries})


def count_by_kind(entries: list[AuditEntry], kind: str) -> int:
    """统计某动作类型的条数。"""

    return len(by_kind(entries, kind))
