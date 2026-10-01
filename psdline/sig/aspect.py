"""灯位显示。

发车灯亮（绿）优先于到站灯亮（黄）；两者都没亮时显示红光带。显示只读灯位文件，
不做任何状态推导，这样控制台看到的颜色与实际灯位永远一致。
"""

from __future__ import annotations

from .sig import SignalController


def aspect_of(signals: SignalController, train_id: str) -> str:
    """返回列车的灯位显示。"""

    if signals.is_raised(train_id, "depart"):
        return "green"
    if signals.is_raised(train_id, "arrival"):
        return "yellow"
    return "red"


def describe(signals: SignalController, train_id: str) -> dict:
    """返回灯位显示及其原始灯位。"""

    return {
        "train_id": train_id,
        "aspect": aspect_of(signals, train_id),
        "raised": signals.raised_kinds(train_id),
    }
