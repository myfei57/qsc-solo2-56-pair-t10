"""位置可信度。

链路状态映射成位置可信度，供位置刷新、开门与发车放行共用：

- ``trusted``：链路良好，位置照常接受；
- ``reduced``：链路降级，位置接受但打标，开门与发车需人工确认；
- ``untrusted``：彻底失联，位置保持最后可信值（退避），开门与发车拒绝。

没有链路会话的列车返回 ``None``，由调用方走既有路径（兼容未接入链路
监督的旧流程）；一旦有过任何采样，监督永久生效。
"""

from __future__ import annotations

from .quality import DEGRADED, GOOD, LOST

TRUSTED = "trusted"
REDUCED = "reduced"
UNTRUSTED = "untrusted"

_BY_STATE = {GOOD: TRUSTED, DEGRADED: REDUCED, LOST: UNTRUSTED}


def trust_of(state: str) -> str:
    """把链路状态映射成位置可信度。"""

    return _BY_STATE[state]
