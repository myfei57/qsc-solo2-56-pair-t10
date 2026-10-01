"""线路命名空间。

一个运行单元只属于一条线路，线路下挂若干站台标识。``Namespace`` 负责标识合法
性与去重，``NamespaceRegistry`` 负责现场设备的 ``区域/设备/类型`` 三元登记；
控制台、审计与联锁读取的是同一套名字，避免出现"同一个设备三处叫法不同"。
"""

from __future__ import annotations

from .namespace import Namespace
from .registry import DeviceEntry, NamespaceRegistry

__all__ = ["DeviceEntry", "Namespace", "NamespaceRegistry"]
