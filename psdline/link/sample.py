"""链路采样。

车地无线链路的唯一事实来源是采样流：每条车地报文（位置刷新或纯链路心跳）
折算成一条 ``ok`` 采样；控制周期发现某拍没有收到报文时合成一条 ``missed``
采样。采样只追加、不修改，落进分段 JSONL 流水，重启恢复与事后重放都从这
份流水重新折叠。

采样时间一律使用服务端接收时刻（``at``）：车端时钟（``sent_at``）只用于
诊断偏移，不参与窗口计算，避免车端钟漂污染质量判定。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LinkSample:
    """一条链路采样。

    ``note`` 记录采样被如何对待：``""`` 表示正常计入；``duplicate``／``late``
    表示序号重复或乱序，只留证据不计入质量；``clock_skew`` 表示车端时钟超前
    超过容忍，仍计入但打了标记；``collapsed`` 表示停机恢复后合并补记的一串
    欠拍（``count`` 为合并拍数）。
    """

    train_id: str
    seq: int
    ok: bool
    at: float
    origin: str = "telegram"
    counted: bool = True
    note: str = ""
    count: int = 1
    device_seq: int | None = None
    rtt_ms: float | None = None
    sent_at: float | None = None
    skew_s: float | None = None

    def as_dict(self) -> dict:
        return {
            "train_id": self.train_id,
            "seq": self.seq,
            "ok": self.ok,
            "at": self.at,
            "origin": self.origin,
            "counted": self.counted,
            "note": self.note,
            "count": self.count,
            "device_seq": self.device_seq,
            "rtt_ms": self.rtt_ms,
            "sent_at": self.sent_at,
            "skew_s": self.skew_s,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "LinkSample":
        return cls(
            train_id=str(payload.get("train_id", "")),
            seq=int(payload.get("seq", 0)),
            ok=bool(payload.get("ok", False)),
            at=float(payload.get("at", 0.0)),
            origin=str(payload.get("origin", "telegram")),
            counted=bool(payload.get("counted", True)),
            note=str(payload.get("note", "")),
            count=int(payload.get("count", 1)),
            device_seq=(
                int(payload["device_seq"]) if payload.get("device_seq") is not None else None
            ),
            rtt_ms=(float(payload["rtt_ms"]) if payload.get("rtt_ms") is not None else None),
            sent_at=(float(payload["sent_at"]) if payload.get("sent_at") is not None else None),
            skew_s=(float(payload["skew_s"]) if payload.get("skew_s") is not None else None),
        )
