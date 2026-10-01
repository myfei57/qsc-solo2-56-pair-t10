"""站台记录校验。"""

from __future__ import annotations

from ..errors import RailError


def validate_station(station: object) -> None:
    """校验站台记录：标识、名称、线路齐全，门区不空且不重复。"""

    identifier = str(getattr(station, "id", ""))
    name = str(getattr(station, "name", ""))
    line = str(getattr(station, "line", ""))
    groups = list(getattr(station, "door_groups", []))
    if not identifier:
        raise RailError("站台标识为空", component="station")
    if not name:
        raise RailError("站台名称为空", component="station")
    if not line:
        raise RailError("站台线路为空", component="station")
    seen: set[str] = set()
    for group in groups:
        if not group:
            raise RailError("门区标识为空", component="station")
        if group in seen:
            raise RailError(f"门区重复: {group}", component="station")
        seen.add(group)
