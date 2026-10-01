"""位置刷新入口。

列车位置只由信号源刷新：信号侧收到位置报文后写入位置文档，并在越界时拒绝。
返回值里带上是否被接受，便于把拒绝原因落进审计。
"""

from __future__ import annotations

from ..train import PositionTracker


def refresh_position(positions: PositionTracker, train_id: str, position_m: int, at: float) -> dict:
    """刷新一列车的位置。"""

    outcome = positions.update(train_id, position_m, at)
    if not outcome.get("accepted"):
        return {
            "ok": False,
            "train_id": train_id,
            "reason": outcome.get("reason", "rejected"),
            "position_m": outcome.get("position_m", position_m),
        }
    return {"ok": True, "train_id": train_id, "position_m": outcome["position_m"]}
