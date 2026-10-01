"""间隙探测。

发车前必须先确认站台与列车之间没有异物：探测结果按列车保存，"确认"与"可疑"
两种取值都要保留下来，供审计与联锁互查。未对位的列车不接受探测。
"""

from __future__ import annotations

from .gap import BLOCKED, CLEAR, GapRegistry, ProbeResult
from .status import gap_status, gap_summary

__all__ = [
    "BLOCKED",
    "CLEAR",
    "GapRegistry",
    "ProbeResult",
    "gap_status",
    "gap_summary",
]
