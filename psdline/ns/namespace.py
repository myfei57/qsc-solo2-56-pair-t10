"""线路命名空间。

线路与站台标识直接出现在持久化文档键、审计流水与 HTTP 路由里，因此必须在装配期
就校验：标识只能由字母、数字、下划线与连字符组成，且以字母或数字开头；站台列表
重复项会被静默合并，但非法标识会直接拒绝装配。
"""

from __future__ import annotations

import re

from ..errors import NamespaceError

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def is_identifier(value: str) -> bool:
    """判断一个字符串是否能当线路或站台标识。"""

    return bool(IDENTIFIER.match(value))


class Namespace:
    """一条线路及其站台集合。"""

    def __init__(self, line: str, stations: list[str]) -> None:
        if not is_identifier(line):
            raise NamespaceError(f"线路标识非法: {line!r}", component="ns")
        seen: set[str] = set()
        clean: list[str] = []
        for station in stations:
            if not is_identifier(station):
                raise NamespaceError(f"站台标识非法: {station!r}", component="ns")
            if station in seen:
                continue
            seen.add(station)
            clean.append(station)
        self._line = line
        self._stations = tuple(clean)

    @property
    def line(self) -> str:
        return self._line

    def contains(self, station: str) -> bool:
        """判断站台是否属于本线路。"""

        return station in self._stations

    def label(self) -> str:
        """返回 ``线路:站台,站台`` 形式的标签。"""

        return f"{self._line}:{','.join(self._stations)}"

    def line_id(self) -> str:
        return self._line

    def station_ids(self) -> list[str]:
        return list(self._stations)

    def require(self, station: str) -> str:
        """解析站台标识，不属于本线路时抛出异常。"""

        if not self.contains(station):
            raise NamespaceError(f"站台 {station!r} 不属于线路 {self._line}", component="ns")
        return station

    def as_dict(self) -> dict:
        return {"line": self._line, "stations": list(self._stations), "label": self.label()}
