"""门区映射读写。"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import MappingError
from ..store import JsonStore


@dataclass(frozen=True)
class Mapping:
    """一个站台的门区映射。"""

    station_id: str
    groups: dict[str, str]
    revision: int

    def as_dict(self) -> dict:
        return {
            "station_id": self.station_id,
            "groups": dict(sorted(self.groups.items())),
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Mapping":
        groups = payload.get("groups", {})
        clean = (
            {str(key): str(value) for key, value in groups.items()}
            if isinstance(groups, dict)
            else {}
        )
        return cls(
            station_id=str(payload.get("station_id", "")),
            groups=clean,
            revision=int(payload.get("revision", 1)),
        )


@dataclass
class RevisionLog:
    """映射修订历史。"""

    station_id: str
    revisions: list[int] = field(default_factory=list)

    def push(self, revision: int) -> None:
        self.revisions.append(revision)

    def latest(self) -> int:
        return self.revisions[-1] if self.revisions else 0

    def as_dict(self) -> dict:
        return {"station_id": self.station_id, "revisions": list(self.revisions)}


class MappingRegistry:
    """门区映射登记表，落在 ``platform/<station>`` 文档上。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store
        self._rewrites = 0
        self._logs: dict[str, RevisionLog] = {}

    @property
    def rewrites(self) -> int:
        """返回累计重排次数。"""

        return self._rewrites

    @staticmethod
    def _doc_id(station_id: str) -> str:
        return f"platform/{station_id}"

    def current(self, station_id: str) -> Mapping | None:
        """读取当前映射，不存在时返回 ``None``。"""

        raw = self._store.read(self._doc_id(station_id))
        if not isinstance(raw, dict):
            return None
        return Mapping.from_dict(raw)

    def renumber(self, station_id: str, groups: dict[str, str]) -> Mapping:
        """按最新布局重排映射，修订号自增。"""

        if not station_id:
            raise MappingError("映射的站台标识为空", component="platform")
        if not groups:
            raise MappingError("映射不含任何门区", component="platform")
        current = self.current(station_id)
        revision = (current.revision + 1) if current else 1
        mapping = Mapping(station_id=station_id, groups=dict(groups), revision=revision)
        self._store.write(self._doc_id(station_id), mapping.as_dict())
        self._logs.setdefault(station_id, RevisionLog(station_id=station_id)).push(revision)
        self._rewrites += 1
        return mapping

    def history(self, station_id: str) -> RevisionLog:
        """返回某站台的修订历史。"""

        log = self._logs.get(station_id)
        if log is not None:
            return log
        mapping = self.current(station_id)
        log = RevisionLog(station_id=station_id)
        if mapping is not None:
            log.push(mapping.revision)
        self._logs[station_id] = log
        return log

    def revision(self, station_id: str) -> int:
        """返回当前修订号；没有映射时返回 0。"""

        mapping = self.current(station_id)
        return mapping.revision if mapping else 0

    def resolve(self, station_id: str, group_key: str) -> str:
        """把逻辑门区解析成物理门区。"""

        mapping = self.current(station_id)
        if mapping is None:
            raise MappingError(f"站台 {station_id} 没有门区映射", component="platform")
        group = mapping.groups.get(group_key)
        if group is None:
            raise MappingError(
                f"站台 {station_id} 的门区映射没有逻辑键 {group_key}", component="platform"
            )
        return group

    def stations(self) -> list[str]:
        """返回已有映射的站台标识（排序）。"""

        found: list[str] = []
        for doc_id in self._store.list_ids():
            if not doc_id.startswith("platform/") or doc_id.count("/") != 1:
                continue
            found.append(doc_id.split("/", 1)[1])
        return sorted(found)


def resolve(registry: MappingRegistry, station_id: str, group_key: str) -> str:
    """把逻辑门区解析成物理门区。"""

    return registry.resolve(station_id, group_key)
