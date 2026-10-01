"""站台门区映射。

逻辑门区（上位机下发的 ``A`` / ``B``）与物理门区（现场编号）之间的对应关系随
布局更新而变化，所以映射带修订号：每次重排递增一次，联动动作按最新修订执行，
并可用相邻关系把同属一个物理门区的逻辑键找出来。
"""

from __future__ import annotations

from .adjacency import adjacent, shared_zone
from .groups import group_keys, physical_groups, revision_of
from .mapping import Mapping, MappingRegistry, RevisionLog, resolve

__all__ = [
    "Mapping",
    "MappingRegistry",
    "RevisionLog",
    "adjacent",
    "group_keys",
    "physical_groups",
    "resolve",
    "revision_of",
    "shared_zone",
]
