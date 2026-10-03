"""链路状态投影与可信度判定。

状态不是被"改"出来的，而是把一列车的事件流左折叠（fold）出来的：现场推进与
事后重放走同一个 :func:`fold`，保证两条路得到逐位一致的结果。纯时间衰减（断线
越久可信度越低）不需要事件，由 :func:`LinkState.view` 按当前时刻即时计算。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .eventlog import (
    EFFECT_DELIVERED,
    EFFECT_FAILED,
    EFFECT_REQUESTED,
    EPOCH_CHANGED,
    POSITION_APPLIED,
    POSITION_HELD,
    PROBE_SCHEDULED,
    RELEASE_CLOSED,
    RELEASE_DECIDED,
    SAMPLE_RECEIVED,
    SAMPLE_REJECTED,
    LinkEvent,
)
from .policy import LinkPolicy
from .sample import LinkSample

# 接触状态：未知 / 良好 / 勉强（帧能到但质量差）/ 重连确认中 / 失联 / 过期
UNKNOWN = "unknown"
OK = "ok"
MARGINAL = "marginal"
REACQUIRING = "reacquiring"
STALE = "stale"
LOST = "lost"

# 放行模式（降级处置取舍：不禁止开门，但默认锁闭，须人工确认）
AUTO = "auto"          # 链路与位置可信：自动放行
MANUAL = "manual"      # 降级区间：默认锁闭，值班员显式确认后开门/放行
BLOCKED = "blocked"    # 彻底失联/无位置：禁止，仅高权限紧急覆盖可旁路

_RULE_AUTO = "link_confident"
_RULE_MANUAL_AGE = "position_degraded_manual"
_RULE_MANUAL_QUALITY = "link_marginal_manual"
_RULE_BLOCK_LOST = "link_lost"
_RULE_BLOCK_STALE = "position_untrusted"
_RULE_BLOCK_NONE = "no_contact"
_RULE_BLOCK_POSITION = "position_unusable"


@dataclass
class _WindowSample:
    at: float
    score: float


@dataclass
class LinkState:
    """一列车从事件流折叠出的链路/位置/放行状态。"""

    train_id: str
    epoch: str = ""
    epochs_seen: list[str] = field(default_factory=list)
    max_seq: int = -1
    seen_seqs: set[int] = field(default_factory=set)
    last_seen_at: float | None = None
    last_good_at: float | None = None
    last_score: float = 0.0
    window: list[_WindowSample] = field(default_factory=list)
    consecutive_good: int = 0
    was_lost: bool = False
    position_m: int | None = None
    position_at: float | None = None
    position_seq: int | None = None
    rejected: dict[str, int] = field(default_factory=dict)
    received: int = 0
    probes: int = 0
    backoff_level: int = 0
    # 当前未关闭的放行请求
    open_request: dict | None = None
    last_decision: dict | None = None
    # 已登记但未确认投递成功的副作用（部分失败恢复的依据）
    pending_effects: dict[str, dict] = field(default_factory=dict)

    # ---- 派生量（纯函数 + 当前时刻）----------------------------------

    def _trim(self, now: float, window_s: float) -> list[_WindowSample]:
        return [item for item in self.window if now - item.at <= window_s]

    def contact_age(self, now: float) -> float | None:
        if self.last_seen_at is None:
            return None
        return max(0.0, now - self.last_seen_at)

    def view(self, now: float, policy: LinkPolicy) -> dict:
        """返回当前时刻的完整链路视图（可信度、接触状态、位置可信度）。"""

        section = policy.section
        window_items = self._trim(now, section.window_s)
        aggregate = policy.window([(item.at, item.score) for item in window_items])
        age = self.contact_age(now)
        has_contact = age is not None
        age_value = age if age is not None else float("inf")

        if not has_contact:
            contact = UNKNOWN
        elif policy.is_lost(age_value, True):
            contact = LOST
        elif age_value > section.fresh_s:
            contact = STALE
        elif self.was_lost and self.consecutive_good < section.recover_good_samples:
            contact = REACQUIRING
        elif aggregate.samples and aggregate.score < section.window_good_score:
            contact = MARGINAL
        else:
            contact = OK

        confidence = policy.confidence(
            age_value if has_contact else float("inf"), aggregate.score, has_contact
        )
        position_age = None if self.position_at is None else max(0.0, now - self.position_at)
        position_fresh = (
            position_age is not None and position_age <= section.stale_s
        )
        mode, rule = _mode(
            contact, confidence, section.confidence_auto, position_fresh
        )
        return {
            "train_id": self.train_id,
            "contact": contact,
            "mode": mode,
            "rule": rule,
            "confidence": confidence,
            "window_score": aggregate.score,
            "window_samples": aggregate.samples,
            "window_good": aggregate.good,
            "window_good_ratio": round(aggregate.good_ratio, 3),
            "last_seen_age_s": None if age is None else round(age, 3),
            "position_m": self.position_m,
            "position_age_s": None if position_age is None else round(position_age, 3),
            "position_confident": confidence >= section.confidence_auto and position_fresh,
            "position_usable": self.position_m is not None and position_fresh,
            "epoch": self.epoch,
            "last_seq": self.max_seq,
            "consecutive_good": self.consecutive_good,
            "recover_required": section.recover_good_samples,
            "backoff_level": self.backoff_level,
            "rejected": dict(self.rejected),
            "received": self.received,
            "probes": self.probes,
            "has_open_request": self.open_request is not None,
            "open_request": None if self.open_request is None else dict(self.open_request),
            "last_decision": None if self.last_decision is None else dict(self.last_decision),
            "pending_effects": sorted(self.pending_effects),
        }


def _mode(contact: str, confidence: float, auto_threshold: float, position_fresh: bool) -> tuple[str, str]:
    """接触状态 + 可信度 + 位置时效 -> 放行模式与命中规则。"""

    if contact == UNKNOWN:
        return BLOCKED, _RULE_BLOCK_NONE
    if contact == LOST:
        return BLOCKED, _RULE_BLOCK_LOST
    if not position_fresh:
        return BLOCKED, _RULE_BLOCK_POSITION
    if confidence >= auto_threshold and contact in (OK,):
        return AUTO, _RULE_AUTO
    if contact == MARGINAL or contact == REACQUIRING:
        return MANUAL, _RULE_MANUAL_QUALITY
    # 帧仍新鲜但可信度被衰减到自动阈值以下
    return MANUAL, _RULE_MANUAL_AGE


def fold(events: list[LinkEvent], train_id: str) -> LinkState:
    """把一列车的事件流折叠成 :class:`LinkState`。"""

    state = LinkState(train_id=train_id)
    for event in events:
        if event.train_id != train_id:
            continue
        state = apply_event(state, event)
    return state


def apply_event(state: LinkState, event: LinkEvent) -> LinkState:
    """折叠单个事件（现场与重放共用）。"""

    payload = event.payload
    if event.kind == SAMPLE_RECEIVED:
        state.received += 1
        seq = int(payload.get("seq", state.max_seq))
        state.max_seq = max(state.max_seq, seq)
        state.seen_seqs.add(seq)
        # 只保留接收窗口内的序号集合，避免无界增长。
        state.seen_seqs = {s for s in state.seen_seqs if s > state.max_seq - 256}
        state.last_seen_at = event.at
        state.last_score = float(payload.get("score", 0.0))
        state.window.append(_WindowSample(at=event.at, score=state.last_score))
        good = bool(payload.get("good"))
        state.consecutive_good = state.consecutive_good + 1 if good else 0
    elif event.kind == SAMPLE_REJECTED:
        reason = str(payload.get("reason", "bad_payload"))
        state.rejected[reason] = state.rejected.get(reason, 0) + 1
    elif event.kind == EPOCH_CHANGED:
        # 重连：进入"确认中"，退避归零；旧代次迟到帧将在接收处被丢弃。
        new_epoch = str(payload.get("epoch", ""))
        if new_epoch and new_epoch not in state.epochs_seen:
            state.epochs_seen.append(new_epoch)
        state.epoch = new_epoch
        state.max_seq = -1
        state.seen_seqs = set()
        # 跨连接代次后，旧位置属于"上一段链路"，在新代次首个被采信位置到来前
        # 视为无可用位置（但保留字段用于审计追溯）。
        if payload.get("from_epoch"):
            state.position_m = None
            state.position_at = None
            state.position_seq = None
        # 只要此前发生过失联，就保持"重连确认中"，直到连续好采样攒够才解除；
        # 普通代次切换（从未失联）不进入确认态，避免无谓压低可信度。
        state.was_lost = state.was_lost or bool(payload.get("from_lost", False))
        state.consecutive_good = 0
        state.backoff_level = 0
    elif event.kind == POSITION_APPLIED:
        state.position_m = payload.get("position_m")
        state.position_at = event.at
        state.position_seq = payload.get("seq")
    elif event.kind == POSITION_HELD:
        # 位置被链路判定扣留（可信度不足），保留旧值并计数，不覆盖。
        state.rejected["position_held"] = state.rejected.get("position_held", 0) + 1
    elif event.kind == STATE_CHANGED:
        state.backoff_level = int(payload.get("backoff_level", state.backoff_level))
        if "lost" in payload:
            # 失联升档事件置位 was_lost（即使此时新代次尚未出现），恢复事件清位。
            state.was_lost = bool(payload.get("lost"))
    elif event.kind == RELEASE_DECIDED:
        state.last_decision = {
            "request_id": payload.get("request_id"),
            "decision": payload.get("decision"),
            "at": event.at,
            "rule": payload.get("rule"),
        }
        # auto_granted / manual_pending / manual_armed 都是"开放中、等待发车闭环"
        # 的请求；inhibited 只是一次拒绝结论，不占开放位。emergency_override 同样
        # 作为开放授权保留，直到发车关闭。
        if payload.get("decision") in (
            "auto_granted",
            "manual_pending",
            "manual_armed",
            "emergency_override",
        ):
            state.open_request = {
                "request_id": payload.get("request_id"),
                "station_id": payload.get("station_id", ""),
                "decision": payload.get("decision"),
                "at": event.at,
                "rule": payload.get("rule", ""),
                "open_effect": payload.get("open_effect_id", ""),
            }
    elif event.kind == RELEASE_CLOSED:
        if state.open_request and state.open_request.get("request_id") == payload.get("request_id"):
            state.open_request = None
    elif event.kind == PROBE_SCHEDULED:
        state.probes += 1
    elif event.kind == EFFECT_REQUESTED:
        state.pending_effects[str(payload.get("effect_id"))] = {
            "effect_id": payload.get("effect_id"),
            "kind": payload.get("kind"),
            "at": event.at,
        }
    elif event.kind == EFFECT_DELIVERED:
        state.pending_effects.pop(str(payload.get("effect_id")), None)
    elif event.kind == EFFECT_FAILED:
        # 仍留在 pending_effects 中，等待退避重投。
        record = state.pending_effects.get(str(payload.get("effect_id")))
        if record is not None:
            record["last_error"] = payload.get("error", "")
            record["attempts"] = int(record.get("attempts", 1)) + 1
    return state


# 事件类型常量供折叠与投影共用。
STATE_CHANGED = "state_changed"


def link_state(events: list[LinkEvent], train_id: str) -> LinkState:
    """便捷入口：从事件流得到一列车的折叠状态。"""

    return fold(events, train_id)


def train_link_view(
    events: list[LinkEvent], train_id: str, now: float, policy: LinkPolicy
) -> dict:
    """便捷入口：折叠后直接取当前视图。"""

    return fold(events, train_id).view(now, policy)
