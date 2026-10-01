"""运行配额。

每条线路的到发次数有上限，配额按名称（到站 / 发车）分组保存。消费是"先判后扣、
扣减后落盘"，超限时只返回未获准而不改计数；恢复到期的配额用 ``reset``。
"""

from __future__ import annotations

from .quota import Quota, QuotaRegistry

__all__ = ["Quota", "QuotaRegistry"]
