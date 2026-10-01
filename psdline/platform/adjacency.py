"""门区相邻关系。

同一个物理门区可能被多个逻辑键引用（例如双向开门），检修与清扫要把这些逻辑键
一起带上。这里给出"同物理门区"与"相邻物理门区"两个查询。
"""

from __future__ import annotations

from ..errors import MappingError
from .mapping import MappingRegistry


def _group_of(registry: MappingRegistry, station_id: str, group_key: str) -> str:
    mapping = registry.current(station_id)
    if mapping is None:
        raise MappingError(f"站台 {station_id} 没有门区映射", component="platform")
    group = mapping.groups.get(group_key)
    if group is None:
        raise MappingError(f"逻辑键 {group_key} 不存在", component="platform")
    return group


def shared_zone(registry: MappingRegistry, station_id: str, group_key: str) -> list[str]:
    """返回与给定逻辑键指向同一物理门区的全部逻辑键。"""

    mapping = registry.current(station_id)
    if mapping is None:
        raise MappingError(f"站台 {station_id} 没有门区映射", component="platform")
    group = _group_of(registry, station_id, group_key)
    return sorted([key for key, value in mapping.groups.items() if value == group])


def adjacent(registry: MappingRegistry, station_id: str, group_key: str) -> list[str]:
    """返回与给定物理门区编号紧邻的一前一后门区。"""

    mapping = registry.current(station_id)
    if mapping is None:
        raise MappingError(f"站台 {station_id} 没有门区映射", component="platform")
    group = _group_of(registry, station_id, group_key)
    index = _index_of(group)
    if index is None:
        raise MappingError(f"物理门区编号无法解析: {group}", component="platform")
    prefix, number = index
    known = set(mapping.groups.values())
    neighbours = [f"{prefix}{number - 1}", f"{prefix}{number + 1}"]
    return [item for item in neighbours if item in known]


def _index_of(group: str) -> tuple[str, int] | None:
    """把 ``G03`` 拆成 ``("G", 3)``。"""

    prefix = group.rstrip("0123456789")
    digits = group[len(prefix) :]
    if not digits:
        return None
    return prefix, int(digits)
