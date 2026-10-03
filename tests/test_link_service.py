"""车地无线链路服务：掉线重连、乱序、并发、降级放行与重放。

这些用例贯穿"采样 → 位置可信度 → 发车放行/开门 → 审计/事件链"，重点覆盖用户
点名的危险场景：位置不能在断线后停在最后一个值、重连不瞬间恢复高可信、迟到/
乱序帧不污染状态、并发刷新不写花、副作用部分失败可恢复、事后可重放解释。
"""

from __future__ import annotations

import threading

import pytest

from psdline.link import (
    AUTO,
    BLOCKED,
    MANUAL,
    LinkEventLog,
    LinkSample,
    LinkService,
    RecordingSink,
    explain_decision,
    replay_train,
)
from psdline.link import effects as effects_mod
from psdline.link.replay import ReplayCursor, state_as_of
from psdline.store import JsonlJournal


def _service(clock, tmp_path, sinks=None):
    from psdline.config import default_config
    from psdline.eventbus import EventBus
    from psdline.link import EffectOutbox

    journal = JsonlJournal(str(tmp_path), "link", segment_limit=256)
    log = LinkEventLog(journal)
    bus = EventBus()
    sink = RecordingSink()
    outbox = EffectOutbox(log, sinks or {
        "doors_arm": sink, "doors_lock": sink, "signal_hold": sink,
        "signal_green": sink, "link_alarm": sink,
    })
    service = LinkService(default_config().link, clock, log, bus, outbox)
    return service, log, sink, bus


def _make(seq, epoch="e1", at=0.0, **metrics):
    defaults = dict(rssi_dbm=-55.0, snr_db=25.0, rtt_ms=100.0, packet_loss=0.01)
    defaults.update(metrics)
    return LinkSample(
        train_id="T01", seq=seq, epoch=epoch, sent_at=at, at=at,
        position_m=metrics.get("position_m"), **{
            k: v for k, v in defaults.items() if k != "position_m"
        },
    )


def _feed_good(service, clock, count, start=1, epoch="e1", position_base=100):
    for i in range(start, start + count):
        clock.advance(1.0)
        out = service.ingest(_make(i, epoch=epoch, at=clock.now(),
                                   position_m=position_base + i))
    return out


# ---- 掉线 / 重连 / 位置冻结 ---------------------------------------------

def test_outage_decays_confidence_and_freezes_position(clock, tmp_path):
    service, log, sink, bus = _service(clock, tmp_path)
    _feed_good(service, clock, 3)
    view = service.view("T01", clock.now())
    assert view["contact"] == "ok" and view["mode"] == AUTO
    assert view["position_m"] == 103

    # 断线 5s：帧已不新鲜，可信度下降但仍可降级人工。
    clock.advance(5.0)
    view = service.view("T01", clock.now())
    assert view["contact"] == "stale"
    assert view["mode"] == MANUAL
    assert 0.0 < view["confidence"] < 1.0
    assert view["position_m"] == 103  # 位置停在旧值，但可信度已显式降低

    # 断线超过 stale_s：可信度归零、禁止。
    clock.advance(20.0)
    service.tick("T01", clock.now())
    view = service.view("T01", clock.now())
    assert view["contact"] == "lost" and view["mode"] == BLOCKED
    assert view["confidence"] == 0.0
    assert view["position_confident"] is False


def test_held_position_is_not_overwritten_when_degraded(clock, tmp_path):
    from psdline.config import default_config
    service, log, sink, bus = _service(clock, tmp_path)
    _feed_good(service, clock, 3)
    clock.advance(5.0)
    # 一帧很差且已在降级窗口的报文，即便携带新位置也不得采信。
    out = service.ingest(_make(99, at=clock.now(), position_m=999,
                               rssi_dbm=-95, snr_db=2, rtt_ms=950, packet_loss=0.5))
    view = service.view("T01", clock.now())
    assert out["position_written"] is False
    assert view["position_m"] == 103
    assert state_rejected_count(log, "T01", "position_held") == 1


def state_rejected_count(log, train_id, reason):
    state = __import__("psdline.link.state", fromlist=["fold"]).fold(log.for_train(train_id), train_id)
    return state.rejected.get(reason, 0)


def test_reconnect_requires_consecutive_good_samples(clock, tmp_path):
    service, log, sink, bus = _service(clock, tmp_path)
    _feed_good(service, clock, 3)
    clock.advance(25.0)
    service.tick("T01", clock.now())
    assert service.view("T01", clock.now())["contact"] == "lost"

    # 新代次首个好采样：进入重连确认中，不瞬间恢复高可信，但位置随新鲜好帧恢复。
    clock.advance(1.0)
    first = service.ingest(_make(1, epoch="e2", at=clock.now(), position_m=200))
    assert first["contact"] == "reacquiring"
    clock.advance(1.0)
    second = service.ingest(_make(2, epoch="e2", at=clock.now(), position_m=201))
    assert second["contact"] == "reacquiring"
    clock.advance(1.0)
    third = service.ingest(_make(3, epoch="e2", at=clock.now(), position_m=202))
    assert third["contact"] == "ok" and third["confidence"] == 1.0
    assert service.view("T01", clock.now())["mode"] == AUTO


def test_single_flaky_packet_after_outage_does_not_restore_trust(clock, tmp_path):
    service, log, sink, bus = _service(clock, tmp_path)
    _feed_good(service, clock, 3)
    clock.advance(25.0)
    service.tick("T01", clock.now())
    clock.advance(1.0)
    out = service.ingest(_make(1, epoch="e2", at=clock.now()))
    view = service.view("T01", clock.now())
    assert view["contact"] == "reacquiring"
    # 跨代次尚无新位置可用（旧位置已作废），即使帧新鲜也不能自动放行。
    assert view["mode"] == BLOCKED
    assert view["position_m"] is None

    # 再补两帧好采样并带位置后，恢复自动。
    clock.advance(1.0)
    service.ingest(_make(2, epoch="e2", at=clock.now(), position_m=200))
    clock.advance(1.0)
    recovered = service.ingest(_make(3, epoch="e2", at=clock.now(), position_m=201))
    assert recovered["contact"] == "ok"
    assert service.view("T01", clock.now())["mode"] == AUTO


# ---- 掉线重连 / 时序错位 -----------------------------------------------

def test_old_epoch_late_frame_is_rejected(clock, tmp_path):
    service, log, sink, bus = _service(clock, tmp_path)
    _feed_good(service, clock, 3)
    clock.advance(25.0)
    service.tick("T01", clock.now())
    clock.advance(1.0)
    service.ingest(_make(1, epoch="e2", at=clock.now()))
    # 旧连接 e1 的迟到残包：丢弃，且不能改变位置/代次。
    late = service.ingest(_make(77, epoch="e1", at=clock.now(), position_m=1))
    assert late["accepted"] is False and late["reason"] == "stale_epoch"
    view = service.view("T01", clock.now())
    assert view["epoch"] == "e2"


def test_replayed_and_out_of_order_frames_rejected(clock, tmp_path):
    service, log, sink, bus = _service(clock, tmp_path)
    _feed_good(service, clock, 3)
    # 相同序号：重放。
    assert service.ingest(_make(3, at=clock.now()))["reason"] == "replayed_seq"
    # 远低于有界接收窗口下界：真正的乱序旧帧（先把窗口推高）。
    _feed_good(service, clock, 300, start=4)
    assert service.ingest(_make(10, at=clock.now()))["reason"] == "out_of_order"
    assert service.ingest(_make(900, epoch="", at=clock.now()))["reason"] == "empty_epoch"


def test_bounded_reorder_accepts_interleaved_arrival(clock, tmp_path):
    service, log, sink, bus = _service(clock, tmp_path)
    # 并发刷新常见的小范围交错：先到 3，再到 2，应都能接收而非误判乱序。
    assert service.ingest(_make(1, at=clock.now()))["accepted"] is True
    assert service.ingest(_make(3, at=clock.now()))["accepted"] is True
    assert service.ingest(_make(2, at=clock.now()))["accepted"] is True


def test_clock_skew_frame_rejected(clock, tmp_path):
    from psdline.config import default_config
    service, log, sink, bus = _service(clock, tmp_path)
    service.ingest(_make(1, at=clock.now()))
    # 车载时钟比地面接收时刻早 100s，超出漂移限值，拒绝。
    skewed = LinkSample("T01", 2, "e1", clock.now() - 100, clock.now(),
                        -55, 25, 100, 0.01)
    assert service.ingest(skewed)["reason"] == "clock_skew"


# ---- 并发刷新 ----------------------------------------------------------

def test_concurrent_samples_same_train_are_serialized(clock, tmp_path):
    """同一列车的高频并发刷新必须串行、无异常、链不断裂。

    三个线程在同一代次内写互不重叠的序号区间：每帧都应被恰好接收一次。
    """

    service, log, sink, bus = _service(clock, tmp_path)
    errors: list[Exception] = []
    accepted = {"n": 0}
    lock = threading.Lock()

    def worker(base):
        try:
            for i in range(base, base + 20):
                result = service.ingest(_make(i, epoch="e1", at=clock.now()))
                if result.get("accepted"):
                    with lock:
                        accepted["n"] += 1
                else:
                    raise AssertionError(f"帧 {i} 被意外拒绝: {result.get('reason')}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(b,)) for b in (1, 100, 200)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    state = __import__("psdline.link.state", fromlist=["fold"]).fold(log.for_train("T01"), "T01")
    assert accepted["n"] == 60
    assert state.received == 60
    assert state.rejected == {}
    assert state.max_seq == 219
    assert log.verify()["ok"] is True


def test_different_trains_do_not_block_each_other(clock, tmp_path):
    service, log, sink, bus = _service(clock, tmp_path)
    done = threading.Event()

    def slow_train():
        for i in range(1, 6):
            service.ingest(LinkSample(
                "TBIG", i, "e1", clock.now(), clock.now(), -55, 25, 100, 0.01))

    thread = threading.Thread(target=slow_train)
    thread.start()
    # 另一列车立即能写入。
    out = service.ingest(LinkSample(
        "T02", 1, "e1", clock.now(), clock.now(), -55, 25, 100, 0.01))
    thread.join()
    assert out["accepted"] is True
    assert service.view("T02", clock.now())["contact"] == "ok"


# ---- 降级放行 / 开门 / 紧急覆盖 -----------------------------------------

def test_auto_release_when_confident(clock, tmp_path):
    service, log, sink, bus = _service(clock, tmp_path)
    _feed_good(service, clock, 3)
    result = service.request_release("T01", "ST1", clock.now())
    assert result["decision"] == "auto_granted" and result["ok"] is True
    assert "signal_green" in sink.kinds() and "doors_arm" in sink.kinds()


def test_degraded_defaults_locked_requires_manual_confirm(clock, tmp_path):
    service, log, sink, bus = _service(clock, tmp_path)
    _feed_good(service, clock, 3)
    clock.advance(5.0)
    result = service.request_release("T01", "ST1", clock.now())
    assert result["decision"] == "manual_pending"
    assert "doors_lock" in sink.kinds() and "signal_hold" in sink.kinds()
    # 未确认不能开门。
    allowed, rule, _ = service.can_open_doors("T01", clock.now())
    assert allowed is False
    confirm = service.confirm_release("T01", result["request_id"], "zhang", clock.now())
    assert confirm["decision"] == "manual_armed"
    allowed, rule, _ = service.can_open_doors("T01", clock.now())
    assert allowed is True and rule == "manual_confirmed"


def test_confirm_invalid_once_link_goes_lost(clock, tmp_path):
    service, log, sink, bus = _service(clock, tmp_path)
    _feed_good(service, clock, 3)
    clock.advance(5.0)
    pending = service.request_release("T01", "ST1", clock.now())
    clock.advance(30.0)
    service.tick("T01", clock.now())
    # 待确认期间彻底失联：人工确认失效（fail-safe）。
    result = service.confirm_release("T01", pending["request_id"], "zhang", clock.now())
    assert result["ok"] is False


def test_lost_is_blocked_emergency_override_audited(clock, tmp_path):
    service, log, sink, bus = _service(clock, tmp_path)
    _feed_good(service, clock, 3)
    clock.advance(25.0)
    service.tick("T01", clock.now())
    result = service.request_release("T01", "ST1", clock.now())
    assert result["decision"] == "inhibited" and result["ok"] is False
    bad = service.emergency_override("T01", "ST1", "", "", clock.now())
    assert bad["ok"] is False
    emg = service.emergency_override("T01", "ST1", "supervisor-li", "区间疏散", clock.now())
    assert emg["decision"] == "emergency_override"
    decisions = [e for e in log.for_train("T01") if e.kind == "release_decided"]
    assert any(e.payload.get("decision") == "emergency_override" for e in decisions)


def test_pending_release_auto_upgrades_on_recovery(clock, tmp_path):
    service, log, sink, bus = _service(clock, tmp_path)
    _feed_good(service, clock, 3)
    clock.advance(5.0)
    pending = service.request_release("T01", "ST1", clock.now())
    assert pending["decision"] == "manual_pending"
    # 链路恢复：连续好采样，待确认请求被自动提升，无需人工再确认。
    service.ingest(_make(4, at=clock.now()))  # 仍 stale
    for i in range(5, 8):
        clock.advance(1.0)
        service.ingest(_make(i, at=clock.now()))
    view = service.view("T01", clock.now())
    assert view["open_request"]["decision"] == "auto_granted"


# ---- 部分失败恢复 ------------------------------------------------------

def test_failed_effect_is_redelivered_and_effect_is_idempotent(clock, tmp_path):
    service, log, sink, bus = _service(clock, tmp_path)
    _feed_good(service, clock, 3)
    # 让 signal_green 首投失败。
    sink.fail_on = lambda eid, kind, payload: kind == "signal_green"
    result = service.request_release("T01", "ST1", clock.now())
    pending_state = __import__("psdline.link.state", fromlist=["fold"]).fold(
        log.for_train("T01"), "T01")
    pending_green = [
        eid for eid, rec in pending_state.pending_effects.items()
        if rec["kind"] == "signal_green"
    ]
    assert pending_green
    # 恢复设备后重投：成功，且重投幂等（只施加一次）。
    sink.fail_on = None
    outcome = service.redrive_pending(clock.now())
    assert set(outcome["delivered"]) >= set(pending_green)
    assert service.view("T01", clock.now())["pending_effects"] == []
    green = [e for e in sink.effects() if e["kind"] == "signal_green"]
    assert len(green) == 1


def test_pending_effects_survive_restart(clock, tmp_path):
    from psdline.eventbus import EventBus
    from psdline.config import default_config
    _feed_good(_service(clock, tmp_path)[0], clock, 1)  # 建立列车
    journal = JsonlJournal(str(tmp_path), "link", segment_limit=256)
    log = LinkEventLog(journal)
    sink = RecordingSink()
    sink.fail_on = lambda *a: True  # 所有副作用都投递失败
    from psdline.link import EffectOutbox
    service = LinkService(default_config().link, clock, log, EventBus(),
                          EffectOutbox(log, {k: sink for k in
                                            ("doors_arm", "doors_lock", "signal_hold",
                                             "signal_green", "link_alarm")}))
    _feed_good(service, clock, 2, start=2)
    service.request_release("T01", "ST1", clock.now())
    assert service.view("T01", clock.now())["pending_effects"]

    # 重启：新服务从事件流重建，未确认副作用自动重投（设备已恢复）。
    sink.fail_on = None
    rebuilt_log = LinkEventLog(JsonlJournal(str(tmp_path), "link", segment_limit=256))
    rebuilt_sink = RecordingSink()
    rebuilt = LinkService(
        default_config().link, clock, rebuilt_log, EventBus(),
        EffectOutbox(rebuilt_log, {k: rebuilt_sink for k in
                                   ("doors_arm", "doors_lock", "signal_hold",
                                    "signal_green", "link_alarm")}),
    )
    result = rebuilt.redrive_pending(clock.now())
    assert result["retried"] >= 1 and not result["failed"]


# ---- 重放与解释 --------------------------------------------------------

def test_replay_reproduces_decision_basis(clock, tmp_path):
    service, log, sink, bus = _service(clock, tmp_path)
    _feed_good(service, clock, 3)
    decision_event = None
    result = service.request_release("T01", "ST1", clock.now())
    for event in log.for_train("T01"):
        if event.kind == "release_decided":
            decision_event = event
    explanation = explain_decision(decision_event, log)
    assert explanation["decision"] == "auto_granted"
    assert explanation["policy_version"]
    assert explanation["evidence"]["confidence"] == 1.0
    assert explanation["position_basis"]["position_m"] == 103
    assert explanation["recent_samples"]
    # 哈希链信息可用于逐环核对。
    assert explanation["hash"] == decision_event.hash


def test_replay_train_timeline_deterministic(clock, tmp_path):
    service, log, sink, bus = _service(clock, tmp_path)
    _feed_good(service, clock, 3)
    clock.advance(25.0)
    service.tick("T01", clock.now())
    from psdline.config import default_config
    first = replay_train(log, "T01", default_config().link)
    second = replay_train(log, "T01", default_config().link)
    assert [(e["kind"], e["state"]["contact"]) for e in first] == \
           [(e["kind"], e["state"]["contact"]) for e in second]
    assert any(e["state"]["contact"] == "lost" for e in first)


def test_decision_thresholds_are_snapshotted(clock, tmp_path):
    service, log, sink, bus = _service(clock, tmp_path)
    _feed_good(service, clock, 3)
    service.request_release("T01", "ST1", clock.now())
    event = [e for e in log.for_train("T01") if e.kind == "release_decided"][-1]
    snap = explain_decision(event, log)["thresholds_snapshot"]
    # 阈值固化在事件里：事后改配置也能按当时口径重放。
    assert snap["fresh_s"] == 2.0 and snap["loss_s"] == 20.0
    assert snap["confidence_auto"] == 0.85


def test_state_as_of_and_cursor_reproduce_a_moment(clock, tmp_path):
    from psdline.config import default_config
    service, log, sink, bus = _service(clock, tmp_path)
    _feed_good(service, clock, 3)
    cutoff = len(log)
    clock.advance(25.0)
    service.tick("T01", clock.now())
    # 停在 cutoff：那一刻链路仍然良好，不包含后来的失联。
    state_at = state_as_of(log, "T01", cutoff, default_config().link)
    events = log.for_train("T01")
    view_at = state_at.view(events[cutoff - 1].at, service.policy)
    assert view_at["contact"] == "ok"
    # 游标逐事件走到 cutoff 处状态一致。
    cursor = ReplayCursor.begin("T01", default_config().link)
    for event in log.for_train("T01"):
        if event.seq > cutoff:
            break
        cursor.step(event)
    assert cursor.state.view(events[cutoff - 1].at, cursor.policy)["contact"] == "ok"


def test_sink_has_and_outbox_attach(clock, tmp_path):
    service, log, sink, bus = _service(clock, tmp_path)
    _feed_good(service, clock, 3)
    service.request_release("T01", "ST1", clock.now())
    delivered_ids = {e["effect_id"] for e in sink.effects()}
    green_ids = [e["effect_id"] for e in sink.effects() if e["kind"] == "signal_green"]
    assert green_ids and green_ids[0] in delivered_ids
    # 动态挂载新的接收侧。
    extra = RecordingSink()
    service.outbox.attach("custom_kind", extra)
    service.outbox.deliver_record(clock.now(), "T01",
                                  {"effect_id": "x-1", "kind": "custom_kind", "payload": {}})
    assert any(e["effect_id"] == "x-1" for e in extra.effects())


def test_window_good_ratio():
    agg = LinkPolicy_good_ratio()
    assert 0.0 <= agg <= 1.0


def LinkPolicy_good_ratio():
    from psdline.link import LinkPolicy
    from psdline.config import default_config
    policy = LinkPolicy(default_config().link)
    agg = policy.window([(0.0, 1.0), (1.0, 0.1), (2.0, 0.9)])
    return agg.good_ratio
