"""趋势缓冲与补点。

现场采样可能因为门控动作被抢占而丢点，控制台要展示的是按固定周期对齐的趋势
而不是原始采样流。:class:`TrendBuffer` 按信号名收集样本，并可对缺失周期做线性
补点，供状态页与审计回放共用。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class Sample:
    """一个带时间戳的采样点。"""

    ts: float
    value: float

    def as_dict(self) -> dict:
        return {"ts": round(self.ts, 3), "value": round(self.value, 4)}


class TrendBuffer:
    """按信号名保存有限长度的趋势样本。"""

    def __init__(self, limit: int = 2048, period_s: float = 1.0) -> None:
        self._series: dict[str, list[Sample]] = {}
        self._limit = max(1, limit)
        self._period_s = max(0.001, period_s)
        self._lock = threading.RLock()

    @property
    def period_s(self) -> float:
        return self._period_s

    def append(self, signal: str, ts: float, value: float) -> None:
        """追加一个采样点，超出上限时丢弃最旧的样本。"""

        with self._lock:
            series = self._series.setdefault(signal, [])
            series.append(Sample(ts, float(value)))
            if len(series) > self._limit:
                del series[: len(series) - self._limit]

    def latest(self, signal: str) -> Sample | None:
        """返回某信号的最后一个样本。"""

        with self._lock:
            series = self._series.get(signal)
            return series[-1] if series else None

    def series(self, signal: str, limit: int = 120) -> list[Sample]:
        """返回某信号最近的若干样本。"""

        with self._lock:
            series = list(self._series.get(signal, ()))
        return series[-limit:]

    def signals(self) -> list[str]:
        """返回当前登记的全部信号名。"""

        with self._lock:
            return sorted(self._series)

    def fill_gaps(self, signal: str, samples: list[Sample]) -> list[Sample]:
        """对丢点周期做线性补点，保持趋势按固定周期对齐。"""

        if len(samples) < 2:
            return samples
        filled: list[Sample] = [samples[0]]
        for previous, current in zip(samples, samples[1:]):
            steps = int(round((current.ts - previous.ts) / self._period_s))
            if steps <= 1:
                filled.append(current)
                continue
            for index in range(1, steps):
                ratio = index / steps
                ts = previous.ts + (current.ts - previous.ts) * ratio
                value = previous.value + (current.value - previous.value) * ratio
                filled.append(Sample(ts, value))
            filled.append(current)
        return filled

    def snapshot(self, limit: int = 8) -> dict:
        """汇总各信号的最新样本，供状态快照读取。"""

        with self._lock:
            signals = sorted(self._series)
        report: dict[str, object] = {}
        for signal in signals:
            latest = self.latest(signal)
            report[signal] = {
                "latest": latest.as_dict() if latest else None,
                "recent": [sample.as_dict() for sample in self.series(signal, limit)],
            }
        return report
