"""控制服务的统一异常类型。

现场要区分四类失败：动作顺序或联锁条件不满足（条件满足后即可继续）、列车没
对到位（需要重新定位）、站台门区映射过期或缺失（需要按最新布局重排）、以及
配置与持久化失败（装配期就该拦下的接线问题）。审计流水按异常类型落盘，巡检
时才能一眼分出"条件没到""没对好位"和"接线不对"。
"""

from __future__ import annotations


class RailError(Exception):
    """所有控制服务异常的基类，携带出错的组件名。"""

    def __init__(self, message: str, *, component: str = "psdline") -> None:
        super().__init__(message)
        self.message = message
        self.component = component

    def as_record(self) -> dict:
        """转成审计流水可以直接落盘的一条记录。"""

        return {
            "type": type(self).__name__,
            "component": self.component,
            "message": self.message,
        }


class InterlockError(RailError):
    """顺序或联锁条件不满足，动作被拒绝。"""


class AlignmentError(RailError):
    """列车位置超出对位公差。"""


class MappingError(RailError):
    """站台门区映射缺失、过期或与布局不一致。"""


class StorageError(RailError):
    """持久化读写失败。"""


class ConfigError(RailError):
    """配置缺失或取值非法。"""


class NamespaceError(RailError):
    """命名空间重复注册或解析失败。"""
