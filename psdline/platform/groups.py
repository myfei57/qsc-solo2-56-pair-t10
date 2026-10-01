"""映射内的逻辑键与修订号查询。"""

from __future__ import annotations

from .mapping import MappingRegistry


def group_keys(registry: MappingRegistry, station_id: str) -> list[str]:
    """返回某站台映射里的全部逻辑键（排序）。"""

    mapping = registry.current(station_id)
    if mapping is None:
        return []
    return sorted(mapping.groups)


def revision_of(registry: MappingRegistry, station_id: str) -> int:
    """返回某站台映射的修订号。"""

    return registry.revision(station_id)


def physical_groups(registry: MappingRegistry, station_id: str) -> list[str]:
    """返回某站台映射里出现过的不重复物理门区（排序）。"""

    mapping = registry.current(station_id)
    if mapping is None:
        return []
    return sorted(set(mapping.groups.values()))
