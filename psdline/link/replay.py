"""事后重放与"凭什么这么判"的解释。

:func:`replay_train` 只吃事件流与策略，不碰实时时钟：逐事件折叠，并在每条
``release_decided`` 处用事件自己携带的阈值快照与证据，还原当时的链路视图，从而
回答"当时为什么自动放行/降级人工/禁止"。:func:`explain_decision` 把一次判定的
完整证据链（最近采样、位置、接触时长、连续好采样、命中规则、策略版本）整理成
可读结构；:func:`verify_chain` 校验历史不可变。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import LinkSection, default_config
from . import state as states
from .eventlog import RELEASE_DECIDED, LinkEvent, LinkEventLog, verify_events
from .policy import LinkPolicy
from .state import LinkState, fold


@dataclass
class ReplayCursor:
    """逐事件重放的游标，停在任意事件序号处都能取当时状态。"""

    policy: LinkPolicy
    state: LinkState
    seq: int = 0

    @classmethod
    def begin(cls, train_id: str, section: LinkSection | None = None) -> "ReplayCursor":
        section = section or default_config().link
        return cls(policy=LinkPolicy(section), state=LinkState(train_id=train_id))

    def step(self, event: LinkEvent) -> LinkState:
        if event.train_id == self.state.train_id:
            self.state = states.apply_event(self.state, event)
            self.seq = event.seq
        return self.state


def replay_train(
    log: LinkEventLog, train_id: str, section: LinkSection | None = None
) -> list[dict]:
    """重放一列车的完整事件链，返回每个关键时点的快照。"""

    section = section or default_config().link
    policy = LinkPolicy(section)
    state = LinkState(train_id=train_id)
    timeline: list[dict] = []
    for event in log.for_train(train_id):
        state = states.apply_event(state, event)
        timeline.append({
            "seq": event.seq,
            "at": event.at,
            "kind": event.kind,
            "state": state.view(event.at, policy),
            "payload": dict(event.payload),
        })
    return timeline


def state_as_of(
    log: LinkEventLog, train_id: str, event_seq: int, section: LinkSection | None = None
) -> LinkState:
    """把状态重放到全局事件序号 ``event_seq``（含）为止，用于"那一刻"的追溯。"""

    section = section or default_config().link
    state = LinkState(train_id=train_id)
    for event in log.all():
        if event.seq > event_seq:
            break
        if event.train_id == train_id:
            state = states.apply_event(state, event)
    return state


def explain_decision(event: LinkEvent, log: LinkEventLog) -> dict:
    """解释一条放行判定事件：给出当时的完整证据链。"""

    if event.kind != RELEASE_DECIDED:
        return {"error": "not_a_decision", "event_seq": event.seq}
    payload = event.payload
    evidence = dict(payload.get("evidence", {}))

    # 从事件流里捞出判定之前最近的接收采样与位置采信，作为"当时看到了什么"。
    prior_samples: list[dict] = []
    position_basis: dict | None = None
    for prior in log.all():
        if prior.seq >= event.seq:
            break
        if prior.train_id != event.train_id:
            continue
        if prior.kind == "sample_received":
            prior_samples.append({
                "seq": prior.seq,
                "at": prior.at,
                "age_before_decision_s": round(event.at - prior.at, 3),
                "score": prior.payload.get("score"),
                "good": prior.payload.get("good"),
            })
        if prior.kind == "position_applied":
            # 取判定前最近一次被采信的位置（持续覆盖为最新依据）。
            position_basis = {
                "seq": prior.seq,
                "at": prior.at,
                "position_m": prior.payload.get("position_m"),
                "age_before_decision_s": round(event.at - prior.at, 3),
                "confidence_at_apply": prior.payload.get("confidence"),
            }

    return {
        "event_seq": event.seq,
        "at": event.at,
        "train_id": event.train_id,
        "request_id": payload.get("request_id"),
        "station_id": payload.get("station_id", ""),
        "decision": payload.get("decision"),
        "rule": payload.get("rule"),
        "policy_version": payload.get("policy_version"),
        "operator": payload.get("operator"),
        "supervisor": payload.get("supervisor"),
        "reason_text": payload.get("reason"),
        "evidence": evidence,
        "thresholds_snapshot": payload.get("thresholds", evidence.get("thresholds", {})),
        "position_basis": position_basis,
        "recent_samples": prior_samples[-5:],
        "hash": event.hash,
        "prev_hash": event.prev_hash,
    }


def decisions(log: LinkEventLog, train_id: str = "") -> list[LinkEvent]:
    """列出全部（或某列车的）放行判定事件，按事件序号升序。"""

    events = log.for_train(train_id) if train_id else log.all()
    return [event for event in events if event.kind == RELEASE_DECIDED]


def verify_chain(log: LinkEventLog) -> dict:
    """校验整条事件链不可变（顺序连续 + 哈希链完整）。"""

    with log._lock:  # noqa: SLF001 - 校验需要在一致快照上进行
        events = list(log.all())
        verify_events(events)
        return {"ok": True, "events": len(events), "tail": log.tail_hash()}
