"""车地无线链路质量（link）。

这条子系统把「车地链路采样 → 位置可信度 → 发车放行（含开门联锁）→ 审计」串成
一条链，并满足四个硬要求：掉线重连可识别、乱序/迟到报文不污染状态、并发刷新按
列车串行、副作用失败可重放恢复；所有判定只追加、不改写，事后可凭事件逐条重放
"当时为什么这么判"。

口径上的取舍（见 :mod:`psdline.config` 的 ``LinkSection``）：

* 可信度以**断线时长为主、近窗链路质量为辅**。纯近窗口径在彻底断线后窗口冻结、
  可信度永远不掉，恰好重演"位置停在最后一个值、看着像车没动"的事故；以断线时长
  驱动，可信度在断联后必然随时间衰减到 0，且重连后要连续若干个好采样才爬升，
  单个迟到/伪造报文无法瞬间恢复高可信。
* 降级期间**不禁止开门但默认锁闭**：可信度落到手动区间时，必须值班员显式确认才
  能开门/放行，确认只对当次有效；彻底失联则禁止，仅高权限紧急覆盖可旁路，两种
  人工动作都强审计。
"""

from .sample import LinkSample, sample_score
from .policy import LinkPolicy
from .eventlog import LinkEvent, LinkEventLog, TamperError
from .state import (
    AUTO,
    BLOCKED,
    MANUAL,
    LinkState,
    link_state,
    train_link_view,
)
from .service import LinkService
from .gate import GATE_ADOPTED, GATE_INACTIVE, LinkGate
from .effects import EffectOutbox, RecordingSink
from .replay import ReplayCursor, explain_decision, replay_train, state_as_of, verify_chain

GATE_VERSION = "link-gate-v1"

__all__ = [
    "LinkSample",
    "sample_score",
    "LinkPolicy",
    "LinkEvent",
    "LinkEventLog",
    "TamperError",
    "LinkState",
    "link_state",
    "train_link_view",
    "LinkService",
    "LinkGate",
    "GATE_ADOPTED",
    "GATE_INACTIVE",
    "EffectOutbox",
    "RecordingSink",
    "ReplayCursor",
    "explain_decision",
    "replay_train",
    "state_as_of",
    "verify_chain",
    "AUTO",
    "MANUAL",
    "BLOCKED",
    "GATE_VERSION",
]
