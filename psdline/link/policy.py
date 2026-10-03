"""链路判定策略。

策略是一份只读阈值表，外加几个纯计算（窗口聚合、可信度衰减、退避时延）。判定
结果里会带上 :attr:`version` 与全部阈值，事后重放时即使现场后来改过配置，也能
凭事件里固化的阈值还原"当时是按哪套口径判的"。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, asdict

from ..config import LinkSection

POLICY_VERSION = "link-policy-v1"


@dataclass(frozen=True)
class WindowAggregate:
    """最近一个时间窗内的链路质量聚合。"""

    score: float
    samples: int
    good: int

    @property
    def good_ratio(self) -> float:
        return self.good / self.samples if self.samples else 0.0


class LinkPolicy:
    """链路判定阈值与纯计算。"""

    version = POLICY_VERSION

    def __init__(self, section: LinkSection) -> None:
        self._s = section

    @property
    def section(self) -> LinkSection:
        return self._s

    @property
    def thresholds(self) -> dict:
        return dict(asdict(self._s))

    # ---- 窗口与可信度 -------------------------------------------------

    def window(self, scored: list[tuple[float, float]]) -> WindowAggregate:
        """聚合窗口内 ``(到达时刻, 单采样分)`` 序列。"""

        if not scored:
            return WindowAggregate(score=0.0, samples=0, good=0)
        scores = [score for _at, score in scored]
        good = sum(1 for score in scores if score >= self._s.sample_good_score)
        return WindowAggregate(
            score=round(sum(scores) / len(scores), 6),
            samples=len(scores),
            good=good,
        )

    def confidence(self, age_s: float, window_score: float, has_contact: bool) -> float:
        """按"断线时长为主、窗口质量为辅"折算位置可信度 ``[0, 1]``。

        * 无任何报文（``has_contact=False``，例如从未上报）：可信度为 0；
        * 最近一帧在 ``fresh_s`` 内：可信度取窗口分（链路好即为 1）；
        * 超过 ``fresh_s``：先按窗口分起步，再随断线时长在 ``fresh_s..stale_s``
          线性衰减到 0；
        * 超过 ``stale_s``：可信度恒为 0（无论窗口里曾经多好）。
        """

        if not has_contact:
            return 0.0
        if age_s <= self._s.fresh_s:
            return round(window_score, 6)
        if age_s >= self._s.stale_s:
            return 0.0
        ratio = (self._s.stale_s - age_s) / (self._s.stale_s - self._s.fresh_s)
        return round(window_score * max(0.0, ratio), 6)

    def is_lost(self, age_s: float, has_contact: bool) -> bool:
        """报文中断超过 ``loss_s`` 即判彻底失联。"""

        return has_contact and age_s > self._s.loss_s

    # ---- 退避 ---------------------------------------------------------

    def backoff_delay(self, level: int, train_id: str, attempt: int) -> float:
        """指数退避时延（带确定性抖动）。

        抖动不使用随机数，而是由 ``(列车, 尝试序号)`` 哈希出来：重放时同一条
        事件链必然得到同一个退避表，避免"现场与重放对不上"。
        """

        level = max(0, int(level))
        base = min(
            self._s.backoff_max_s,
            self._s.backoff_base_s * (self._s.backoff_factor ** level),
        )
        if self._s.backoff_jitter_pct <= 0:
            return round(base, 6)
        digest = hashlib.sha256(f"{train_id}:{attempt}".encode("utf-8")).digest()
        unit = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF  # [0, 1)
        factor = 1.0 + (unit * 2.0 - 1.0) * self._s.backoff_jitter_pct
        # 抖动后仍不得超过配置上限，避免退避越界。
        return round(min(self._s.backoff_max_s, max(0.0, base * factor)), 6)

    def skew_s(self, sample_sent_at: float, received_at: float) -> float:
        return abs(received_at - sample_sent_at)
