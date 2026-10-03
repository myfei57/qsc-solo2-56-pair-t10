"""链路质量评分与状态判定（纯函数）。

质量分按「最近 ``window_s`` 秒」的滑动窗口计算：分子是窗口内成功采样数，
分母是窗口被覆盖时长内按期望周期应到的拍数（冷启动不足一拍按一拍算）。
这样列车彻底沉默时，随着欠拍合成，质量分会沿窗口自然下滑，而不是停在
最后一个值上。

状态判定分两个方向：

- 向下（变差）立即生效：质量跌破阈值或静默超过 ``lost_after_s`` 马上降级；
- 向上（恢复）带迟滞：必须连续 ``recover_samples`` 个好采样，且从彻底失联
  恢复时还要满足退避观察期，避免边缘抖动的链路在可信与不可信之间来回跳。

监控器、重启恢复与事后重放共用这一份实现，保证「当时凭什么这么判」可以
被原样重算。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

GOOD = "good"
DEGRADED = "degraded"
LOST = "lost"

STATES = (GOOD, DEGRADED, LOST)

_RANK = {GOOD: 0, DEGRADED: 1, LOST: 2}


@dataclass(frozen=True)
class LinkTuning:
    """链路质量限值（由配置小节映射而来）。"""

    expected_period_s: float = 1.0
    window_s: float = 10.0
    good_threshold: float = 0.8
    degraded_threshold: float = 0.5
    lost_after_s: float = 5.0
    recover_samples: int = 3
    backoff_base_s: float = 2.0
    backoff_max_s: float = 30.0
    max_clock_skew_s: float = 2.0


def rank(state: str) -> int:
    """状态严重度：数值越大越差。"""

    return _RANK[state]


def expected_samples(cover_s: float, tuning: LinkTuning) -> int:
    """窗口被覆盖的时长内应到的采样拍数（含窗口两端，至少一拍）。"""

    cover = min(max(cover_s, 0.0), tuning.window_s)
    return max(1, int(math.floor(cover / tuning.expected_period_s + 1e-6)) + 1)


def window_quality(ok_count: int, cover_s: float, tuning: LinkTuning) -> float:
    """滑动窗口质量分：成功采样数 / 应到拍数，截断到 [0, 1]。"""

    expected = expected_samples(cover_s, tuning)
    return min(1.0, max(0.0, ok_count / expected))


def classify(quality: float, silence_s: float, tuning: LinkTuning) -> str:
    """按质量分与静默时长给出即时目标状态（向下方向）。"""

    if silence_s >= tuning.lost_after_s or quality < tuning.degraded_threshold:
        return LOST
    if quality < tuning.good_threshold:
        return DEGRADED
    return GOOD


def backoff_interval(level: int, tuning: LinkTuning) -> float:
    """退避观察期：每再失联一次翻一倍，封顶 ``backoff_max_s``。"""

    if level <= 0:
        return 0.0
    return min(tuning.backoff_max_s, tuning.backoff_base_s * (2 ** (level - 1)))
