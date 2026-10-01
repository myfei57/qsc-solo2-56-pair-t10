"""现场设备命名空间登记。

控制台要按"区域—设备—类型"检索现场点位，审计流水里要把动作落回具体设备，因此
登记表按地址索引，并保留类型索引供页面按类型分组展示。重复登记同一地址会直接
报错：现场出现两个同名设备是无法排查的事故源。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import NamespaceError


@dataclass(frozen=True)
class DeviceEntry:
    """一个现场设备点位。"""

    area: str
    device: str
    kind: str

    @property
    def address(self) -> str:
        """返回 ``区域/设备`` 形式的地址。"""

        return f"{self.area}/{self.device}"

    def as_dict(self) -> dict:
        return {"area": self.area, "device": self.device, "kind": self.kind, "address": self.address}


class NamespaceRegistry:
    """现场设备点位登记表。"""

    def __init__(self, unit: str) -> None:
        if not unit:
            raise NamespaceError("运行单元不能为空", component="ns")
        self._unit = unit
        self._entries: dict[str, DeviceEntry] = {}

    @property
    def unit(self) -> str:
        return self._unit

    def register(self, area: str, device: str, kind: str) -> DeviceEntry:
        """登记一个设备点位，地址重复时拒绝。"""

        if not area or not device or not kind:
            raise NamespaceError("区域、设备与类型都不能为空", component="ns")
        entry = DeviceEntry(area=area, device=device, kind=kind)
        if entry.address in self._entries:
            raise NamespaceError(f"设备地址重复登记: {entry.address}", component="ns")
        self._entries[entry.address] = entry
        return entry

    def resolve(self, area: str, device: str) -> DeviceEntry:
        """按区域与设备解析一个点位。"""

        address = f"{area}/{device}"
        entry = self._entries.get(address)
        if entry is None:
            raise NamespaceError(f"设备点位未登记: {address}", component="ns")
        return entry

    def kinds(self) -> list[str]:
        """返回出现过的设备类型（去重排序）。"""

        return sorted({entry.kind for entry in self._entries.values()})

    def by_kind(self, kind: str) -> list[DeviceEntry]:
        """返回某一类型的全部点位。"""

        return [entry for entry in self._entries.values() if entry.kind == kind]

    def all(self) -> list[DeviceEntry]:
        """返回全部点位（按地址排序）。"""

        return [self._entries[key] for key in sorted(self._entries)]

    def count(self) -> int:
        return len(self._entries)

    def describe(self) -> dict:
        """返回可供控制台直接渲染的登记表快照。"""

        return {
            "unit": self._unit,
            "count": len(self._entries),
            "kinds": self.kinds(),
            "devices": [entry.as_dict() for entry in self.all()],
        }
