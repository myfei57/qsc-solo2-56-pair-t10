"""控制台 HTTP 服务。

四个页面给值班员看：站台页看门区与布局、列车页看到发与对位、门控页看开闭与
锁定、告警页看故障与审计。一组 JSON 接口给图形化客户端用。服务用标准库
``http.server`` 承载，不引入额外依赖。
"""

from __future__ import annotations

from .api import Api
from .pages import PAGE_PATHS, render_page
from .router import Route, Router
from .server import ConsoleServer, probe_endpoints

__all__ = [
    "Api",
    "ConsoleServer",
    "PAGE_PATHS",
    "Route",
    "Router",
    "probe_endpoints",
    "render_page",
]
