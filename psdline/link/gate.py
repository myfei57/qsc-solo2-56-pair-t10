"""发车放行闸门（链路降级处置）。

闸门本身是纯判定：读一列车的链路视图，回答"这列车现在该自动放行、降级人工、还是
禁止"，以及命中了哪条规则。它不写状态、不发副作用，副作用与审计由
:class:`~psdline.link.service.LinkService` 统一编排，保证判定与留痕是同一套逻辑。

处置取舍：降级（``manual``）**不禁止开门但默认锁闭**——必须值班员对当次发车显式
确认（:meth:`confirm`），才放开开门联锁与发车信号；彻底失联（``blocked``）则禁止，
仅高权限紧急覆盖（:meth:`emergency_override`）可旁路，两种人工动作都会被强审计。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import LinkSection
from . import state as states

GATE_ADOPTED = "adopted"
GATE_INACTIVE = "inactive"

DECISION_AUTO = "auto_granted"
DECISION_MANUAL_ARMED = "manual_armed"
DECISION_MANUAL_PENDING = "manual_pending"
DECISION_INHIBITED = "inhibited"
DECISION_EMERGENCY = "emergency_override"
DECISION_CLOSED = "closed"

# 闸门对外部（门控/信号/告警）下发的副作用类型
EFFECT_ARM_DOORS = "doors_arm"          # 允许人工开门（门控解锁，仅当次）
EFFECT_LOCK_DOORS = "doors_lock"        # 默认锁闭门控
EFFECT_SIGNAL_HOLD = "signal_hold"      # 扣住发车信号
EFFECT_SIGNAL_GREEN = "signal_green"    # 放开/升起发车信号
EFFECT_ALARM = "link_alarm"             # 降级/失联告警

_MODE_DECISION = {
    states.AUTO: DECISION_AUTO,
    states.MANUAL: DECISION_MANUAL_PENDING,
    states.BLOCKED: DECISION_INHIBITED,
}

_MODE_TEXT = {
    states.AUTO: "链路与位置可信，自动放行",
    states.MANUAL: "链路降级，位置可信度不足，默认锁闭：须值班员当次确认",
    states.BLOCKED: "彻底失联或位置不可信：禁止放行，仅高权限紧急覆盖可旁路",
}


@dataclass(frozen=True)
class GateVerdict:
    """一次放行判定。"""

    mode: str
    decision: str
    rule: str
    reason: str
    confidence: float
    contact: str
    position_m: int | None
    adopted: bool

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "decision": self.decision,
            "rule": self.rule,
            "reason": self.reason,
            "confidence": self.confidence,
            "contact": self.contact,
            "position_m": self.position_m,
            "adopted": self.adopted,
        }


class LinkGate:
    """发车/开门放行的纯判定闸门。"""

    def __init__(self, section: LinkSection) -> None:
        self._s = section

    @property
    def thresholds(self) -> dict:
        return {"confidence_auto": self._s.confidence_auto,
                "confidence_manual": self._s.confidence_manual}

    def evaluate(self, view: dict, adopted: bool = True) -> GateVerdict:
        """根据链路视图判定放行档位。

        ``adopted=False`` 表示该列车尚未纳入链路管理（从未上报），此时闸门不
        介入，维持既有作业流程（对存量无链路设备的车保持兼容）。
        """

        if not adopted:
            return GateVerdict(
                mode=states.AUTO,
                decision=DECISION_AUTO,
                rule=GATE_INACTIVE,
                reason="该列车未纳入链路质量管控，闸门不介入",
                confidence=1.0,
                contact=states.UNKNOWN,
                position_m=view.get("position_m"),
                adopted=False,
            )
        mode = str(view.get("mode", states.BLOCKED))
        return GateVerdict(
            mode=mode,
            decision=_MODE_DECISION.get(mode, DECISION_INHIBITED),
            rule=str(view.get("rule", "")),
            reason=_MODE_TEXT.get(mode, "禁止放行"),
            confidence=float(view.get("confidence", 0.0)),
            contact=str(view.get("contact", states.UNKNOWN)),
            position_m=view.get("position_m"),
            adopted=True,
        )

    def can_open_doors(self, view: dict, confirmed: bool, adopted: bool = True) -> tuple[bool, str]:
        """开门联锁：自动档可开；降级档须当次已确认；失联禁止（紧急覆盖另走）。"""

        if not adopted:
            return True, GATE_INACTIVE
        verdict = self.evaluate(view, adopted=True)
        if verdict.mode == states.AUTO:
            return True, verdict.rule
        if verdict.mode == states.MANUAL and confirmed:
            return True, "manual_confirmed"
        return False, verdict.rule
