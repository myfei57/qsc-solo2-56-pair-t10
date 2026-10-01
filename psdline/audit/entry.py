"""审计条目。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AuditEntry:
    """一条审计流水。"""

    id: str
    kind: str
    train_id: str
    station_id: str
    at: float
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "train_id": self.train_id,
            "station_id": self.station_id,
            "at": self.at,
            "detail": dict(self.detail),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "AuditEntry":
        detail = payload.get("detail", {})
        return cls(
            id=str(payload.get("id", "")),
            kind=str(payload.get("kind", "")),
            train_id=str(payload.get("train_id", "")),
            station_id=str(payload.get("station_id", "")),
            at=float(payload.get("at", 0.0)),
            detail=dict(detail) if isinstance(detail, dict) else {},
        )
