"""车地无线链路采样与单采样打分。

一个采样是车地无线在某一时刻上报的一帧链路质量（信号强度、信噪比、往返时延、
丢包率），并可顺带携带该帧观测到的列车位置。打分是纯函数，只依赖策略阈值，不
读写任何状态，保证现场打分与事后重放打分逐位一致。

注意"好/坏"方向不同：RSSI、SNR 越大越好，RTT、丢包率越小越好。
"""

from __future__ import annotations

from dataclasses import dataclass

from .policy import LinkPolicy


@dataclass(frozen=True)
class LinkSample:
    """一帧车地无线链路采样。

    ``seq`` 为车地无线在一个连接代次（``epoch``）内单调递增的帧序号；``epoch``
    在每次掉线重连后由车载侧换新，旧代次的迟到帧据此被丢弃。``sent_at`` 是车载
    时钟，``at`` 是地面接收时刻，二者差值用于识别时钟漂移与迟到帧。
    """

    train_id: str
    seq: int
    epoch: str
    sent_at: float
    at: float
    rssi_dbm: float
    snr_db: float
    rtt_ms: float
    packet_loss: float
    position_m: int | None = None

    def as_dict(self) -> dict:
        return {
            "train_id": self.train_id,
            "seq": self.seq,
            "epoch": self.epoch,
            "sent_at": self.sent_at,
            "at": self.at,
            "rssi_dbm": self.rssi_dbm,
            "snr_db": self.snr_db,
            "rtt_ms": self.rtt_ms,
            "packet_loss": self.packet_loss,
            "position_m": self.position_m,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "LinkSample":
        position = payload.get("position_m")
        return cls(
            train_id=str(payload.get("train_id", "")),
            seq=int(payload.get("seq", 0)),
            epoch=str(payload.get("epoch", "")),
            sent_at=float(payload.get("sent_at", 0.0)),
            at=float(payload.get("at", 0.0)),
            rssi_dbm=float(payload.get("rssi_dbm", 0.0)),
            snr_db=float(payload.get("snr_db", 0.0)),
            rtt_ms=float(payload.get("rtt_ms", 0.0)),
            packet_loss=float(payload.get("packet_loss", 0.0)),
            position_m=None if position is None else int(position),
        )


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _higher_better(value: float, good: float, bad: float) -> float:
    """RSSI/SNR 这类越大越好的指标，在 good..bad 之间线性折算。"""

    if good == bad:
        return 1.0 if value >= good else 0.0
    return _clamp01((value - bad) / (good - bad))


def _lower_better(value: float, good: float, bad: float) -> float:
    """RTT/丢包率这类越小越好的指标，在 good..bad 之间线性折算。"""

    if good == bad:
        return 1.0 if value <= good else 0.0
    return _clamp01((bad - value) / (bad - good))


def sample_score(sample: LinkSample, policy: LinkPolicy) -> float:
    """把一帧采样折算成 ``[0, 1]`` 的单采样链路质量分。

    四个指标加权平均：信号强度 0.30、信噪比 0.20、往返时延 0.20、丢包率 0.30。
    丢包率直接反映"帧到没到"，权重最高。
    """

    s = policy.section
    parts = (
        (0.30, _higher_better(sample.rssi_dbm, s.rssi_good_dbm, s.rssi_bad_dbm)),
        (0.20, _higher_better(sample.snr_db, s.snr_good_db, s.snr_bad_db)),
        (0.20, _lower_better(sample.rtt_ms, s.rtt_good_ms, s.rtt_bad_ms)),
        (0.30, _lower_better(sample.packet_loss, s.loss_good, s.loss_bad)),
    )
    return round(sum(weight * value for weight, value in parts), 6)
