"""车地链路质量监督。

车地无线一断，位置刷新就停在最后一个值上，看着像车没动。本包把链路变成
一条可监督的控制链：按列车采样链路质量，质量差就降低位置可信度，彻底失
联就退避（位置保持最后可信值并明确标注），发车放行与开门按降级处理；
所有判定随采样落盘，事后可原样重放。
"""

from __future__ import annotations

from .monitor import LinkMonitor, Session
from .quality import (
    DEGRADED,
    GOOD,
    LOST,
    LinkTuning,
    backoff_interval,
    classify,
    expected_samples,
    window_quality,
)
from .replay import replay_train
from .sample import LinkSample
from .trust import REDUCED, TRUSTED, UNTRUSTED, trust_of

__all__ = [
    "DEGRADED",
    "GOOD",
    "LOST",
    "REDUCED",
    "TRUSTED",
    "UNTRUSTED",
    "LinkMonitor",
    "LinkSample",
    "LinkTuning",
    "Session",
    "backoff_interval",
    "classify",
    "expected_samples",
    "replay_train",
    "trust_of",
    "window_quality",
]
