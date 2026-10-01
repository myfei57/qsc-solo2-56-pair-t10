"""控制服务配置。

配置分三类：平台（名称、运行单元、存储目录）、现场限值（对位公差、探测确认时间、
批量上限、配额与序号模数）、以及控制台监听地址。所有取值在装配期校验，非法配置
直接抛 :class:`~psdline.errors.ConfigError`，不允许带着默认值悄悄跑起来。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace

from .errors import ConfigError


@dataclass(frozen=True)
class PlatformSection:
    """平台标识与存储。"""

    name: str = "psdline"
    unit: str = "line1"
    storage_root: str = "var/state"


@dataclass(frozen=True)
class NamespaceSection:
    """线路与站台。"""

    line: str = "L1"
    stations: tuple[str, ...] = ("ST1", "ST2", "ST3")


@dataclass(frozen=True)
class StationSection:
    """站台与门区布置。"""

    groups_per_station: int = 4
    doors_per_group: int = 6
    group_span_m: float = 24.0
    stop_tolerance_m: float = 0.8


@dataclass(frozen=True)
class TrainSection:
    """列车限值。"""

    length_m: float = 118.0
    max_position_m: int = 240
    dwell_default_s: float = 45.0
    dwell_max_s: float = 600.0


@dataclass(frozen=True)
class DoorSection:
    """门控限值。"""

    batch_limit: int = 64
    open_hold_s: float = 30.0
    close_confirm_s: float = 6.0
    lockout_release_s: float = 12.0


@dataclass(frozen=True)
class GapSection:
    """间隙探测限值。"""

    confirm_s: float = 4.0
    probe_timeout_s: float = 20.0
    min_clearance_mm: float = 12.0


@dataclass(frozen=True)
class SignalSection:
    """信号刷新限值。"""

    refresh_period_s: float = 1.0
    stale_after_s: float = 8.0
    sequence_modulus: int = 65536


@dataclass(frozen=True)
class InterlockSection:
    """联锁限值。"""

    hold_after_fault_s: float = 3.0
    max_locked_ratio: float = 0.5


@dataclass(frozen=True)
class QuotaSection:
    """运行配额。"""

    arrival_limit: int = 1000
    depart_limit: int = 1000


@dataclass(frozen=True)
class AuditSection:
    """审计流水分段。"""

    journal_segment: int = 512


@dataclass(frozen=True)
class ConsoleSection:
    """控制台监听地址。"""

    host: str = "127.0.0.1"
    port: int = 8080


@dataclass(frozen=True)
class PlatformConfig:
    """一份完整的控制服务配置。"""

    platform: PlatformSection = field(default_factory=PlatformSection)
    namespace: NamespaceSection = field(default_factory=NamespaceSection)
    station: StationSection = field(default_factory=StationSection)
    train: TrainSection = field(default_factory=TrainSection)
    door: DoorSection = field(default_factory=DoorSection)
    gap: GapSection = field(default_factory=GapSection)
    signal: SignalSection = field(default_factory=SignalSection)
    interlock: InterlockSection = field(default_factory=InterlockSection)
    quota: QuotaSection = field(default_factory=QuotaSection)
    audit: AuditSection = field(default_factory=AuditSection)
    console: ConsoleSection = field(default_factory=ConsoleSection)

    @property
    def name(self) -> str:
        return self.platform.name

    @property
    def storage_root(self) -> str:
        return self.platform.storage_root

    def validate(self) -> "PlatformConfig":
        """校验配置，返回自身以便链式调用。"""

        if not self.platform.name:
            raise ConfigError("平台名称不能为空", component="config")
        if not self.platform.unit:
            raise ConfigError("运行单元不能为空", component="config")
        if not self.platform.storage_root:
            raise ConfigError("存储目录不能为空", component="config")
        if not self.namespace.stations:
            raise ConfigError("线路至少需要一个站台", component="config")
        if self.station.groups_per_station <= 0:
            raise ConfigError("每站门区数必须为正", component="config")
        if self.station.doors_per_group <= 0:
            raise ConfigError("每门区门数必须为正", component="config")
        if self.station.stop_tolerance_m <= 0:
            raise ConfigError("对位公差必须为正", component="config")
        if self.train.max_position_m <= 0:
            raise ConfigError("列车位置上限必须为正", component="config")
        if self.train.dwell_max_s <= 0:
            raise ConfigError("停站时长上限必须为正", component="config")
        if self.door.batch_limit <= 0:
            raise ConfigError("批量开门上限必须为正", component="config")
        if self.gap.min_clearance_mm < 0:
            raise ConfigError("最小间隙不能为负", component="config")
        if self.signal.stale_after_s <= 0:
            raise ConfigError("位置过期时间必须为正", component="config")
        if self.signal.sequence_modulus != 65536:
            raise ConfigError("序号模数固定为 65536", component="config")
        if not 0 < self.interlock.max_locked_ratio <= 1:
            raise ConfigError("锁定比例上限必须落在 (0, 1]", component="config")
        if self.quota.arrival_limit <= 0 or self.quota.depart_limit <= 0:
            raise ConfigError("运行配额必须为正", component="config")
        if self.audit.journal_segment <= 0:
            raise ConfigError("审计分段长度必须为正", component="config")
        if not 0 < self.console.port <= 65535:
            raise ConfigError("控制台端口必须落在 1..65535", component="config")
        return self


def default_config() -> PlatformConfig:
    """返回默认配置。"""

    return PlatformConfig().validate()


def _section(cls, payload: object):
    """按数据类字段构造配置小节，忽略未知键。"""

    if not isinstance(payload, dict):
        return cls()
    known = {field_name for field_name in cls.__dataclass_fields__}
    kwargs = {key: value for key, value in payload.items() if key in known}
    if cls is NamespaceSection and "stations" in kwargs:
        kwargs["stations"] = tuple(str(item) for item in kwargs["stations"])
    return cls(**kwargs)


def config_to_mapping(config: PlatformConfig) -> dict:
    """把配置对象转成可落盘的字典。"""

    return {
        "platform": {
            "name": config.platform.name,
            "unit": config.platform.unit,
            "storage_root": config.platform.storage_root,
        },
        "namespace": {
            "line": config.namespace.line,
            "stations": list(config.namespace.stations),
        },
        "station": dict(config.station.__dict__),
        "train": dict(config.train.__dict__),
        "door": dict(config.door.__dict__),
        "gap": dict(config.gap.__dict__),
        "signal": dict(config.signal.__dict__),
        "interlock": dict(config.interlock.__dict__),
        "quota": dict(config.quota.__dict__),
        "audit": dict(config.audit.__dict__),
        "console": dict(config.console.__dict__),
    }


def config_from_mapping(payload: dict) -> PlatformConfig:
    """从一个字典构造配置。"""

    if not isinstance(payload, dict):
        raise ConfigError("配置必须是一个 JSON 对象", component="config")
    config = PlatformConfig(
        platform=_section(PlatformSection, payload.get("platform")),
        namespace=_section(NamespaceSection, payload.get("namespace")),
        station=_section(StationSection, payload.get("station")),
        train=_section(TrainSection, payload.get("train")),
        door=_section(DoorSection, payload.get("door")),
        gap=_section(GapSection, payload.get("gap")),
        signal=_section(SignalSection, payload.get("signal")),
        interlock=_section(InterlockSection, payload.get("interlock")),
        quota=_section(QuotaSection, payload.get("quota")),
        audit=_section(AuditSection, payload.get("audit")),
        console=_section(ConsoleSection, payload.get("console")),
    )
    return config.validate()


def load_config(path: str) -> PlatformConfig:
    """从 JSON 文件加载配置。"""

    if not os.path.isfile(path):
        raise ConfigError(f"配置文件不存在: {path}", component="config")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(f"读取配置失败: {error}", component="config") from error
    return config_from_mapping(payload)


def with_storage_root(config: PlatformConfig, storage_root: str) -> PlatformConfig:
    """返回一份替换了存储目录的配置。"""

    if not storage_root:
        raise ConfigError("存储目录不能为空", component="config")
    return replace(config, platform=replace(config.platform, storage_root=storage_root))
