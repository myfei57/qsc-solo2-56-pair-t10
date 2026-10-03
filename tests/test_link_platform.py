"""平台层链路集成：采样、位置冻结、到发放行、重放，走真实 Platform 装配。"""

from __future__ import annotations

import json

import pytest


def _feed(platform, clock, train_id, seq, epoch="e1", position_m=None, **metrics):
    clock.advance(1.0)
    return platform.link_sample(
        train_id, seq, epoch, position_m=position_m, **metrics
    )


def test_link_not_adopted_does_not_change_existing_flow(platform):
    # 从未上报链路采样的存量车：闸门不介入，既有到发链照常。
    platform.arrive("T01", "ST1", 0)
    platform.check_gap("T01")
    assert platform.depart("T01", "ST1")["ok"] is True
    assert platform.link_view("T01")["contact"] == "unknown"


def test_good_link_position_refreshes_and_departure_auto(platform, clock):
    for i in range(1, 4):
        out = _feed(platform, clock, "T01", i, position_m=100 + i)
        assert out["position_written"] is True and out["frozen"] is False
    assert platform.positions.get("T01") == 103
    platform.arrive("T01", "ST1", 0)
    platform.check_gap("T01")
    depart = platform.depart("T01", "ST1")
    assert depart["ok"] is True
    # 链路放行步骤进入了作业步骤链。
    assert any(step["component"] == "link" for step in depart.get("steps", []))


def test_outage_freezes_position_and_blocks_door_and_depart(platform, clock):
    for i in range(1, 4):
        _feed(platform, clock, "T01", i, position_m=i)
    assert platform.positions.get("T01") == 3
    platform.arrive("T01", "ST1", 0)
    # 断联 3s：仍在手动降级区间（帧不新鲜、可信度下降但未归零）。
    clock.advance(3.0)
    platform.link_tick("T01")
    view = platform.link_view("T01")
    assert view["mode"] == "manual"
    assert view["position_m"] == 3 and view["confidence"] < 1.0
    # 开门默认锁闭。
    from psdline.errors import RailError
    with pytest.raises(RailError):
        platform.doors_open("T01", "ST1")
    # 发车被降级拦下，且审计记录了拦截。
    platform.check_gap("T01")
    depart = platform.depart("T01", "ST1")
    assert depart["ok"] is False
    assert any("降级" in v for v in depart["violations"])
    kinds = {entry.kind for entry in platform.audit.list()}
    assert "link_release_blocked" in kinds and "link_doors_blocked" in kinds


def test_manual_confirm_then_depart_passes(platform, clock):
    for i in range(1, 4):
        _feed(platform, clock, "T01", i, position_m=i)
    platform.arrive("T01", "ST1", 0)
    clock.advance(3.0)
    platform.link_tick("T01")
    platform.check_gap("T01")
    blocked = platform.depart("T01", "ST1")
    assert blocked["ok"] is False
    request_id = platform.link_view("T01")["open_request"]["request_id"]
    confirm = platform.confirm_departure("T01", request_id, "dispatcher-wang")
    assert confirm["ok"] is True
    assert platform.depart("T01", "ST1")["ok"] is True
    # 发车完成后授权被回收。
    assert platform.link_view("T01")["has_open_request"] is False


def test_lost_position_is_not_overwritten_by_late_frame(platform, clock):
    for i in range(1, 4):
        _feed(platform, clock, "T01", i, position_m=10 + i)
    clock.advance(25.0)
    platform.link_tick("T01")
    # 同连接的重复帧（可能是设备重发的最后一帧）：拒绝，位置不变。
    duplicate = platform.link_sample("T01", 3, "e1", position_m=9999)
    assert duplicate["accepted"] is False
    assert duplicate["reason"] in ("replayed_seq", "out_of_order")
    assert platform.positions.get("T01") == 13
    assert platform.link_view("T01")["confidence"] == 0.0


def test_cycle_exposes_degraded_and_lost(platform, clock):
    for i in range(1, 4):
        _feed(platform, clock, "T01", i, position_m=i)
    clock.advance(25.0)
    cycle = platform.cycle()
    assert "T01" in cycle["link_lost"]
    assert "T01" in cycle["link_degraded"]


def test_status_and_train_view_carry_link(platform, clock):
    for i in range(1, 4):
        _feed(platform, clock, "T01", i, position_m=i)
    status = platform.status()
    assert status["link"]["T01"]["contact"] == "ok"
    assert status["link_chain"]["ok"] is True
    assert platform.train_view("T01")["link"]["mode"] == "auto"


def test_replay_and_verify_through_platform(platform, clock):
    for i in range(1, 4):
        _feed(platform, clock, "T01", i, position_m=i)
    platform.link_release("T01", "ST1")
    replay = platform.link_replay("T01")
    kinds = [step["kind"] for step in replay["timeline"]]
    assert "sample_received" in kinds and "release_decided" in kinds
    assert platform.link_verify()["ok"] is True
    decision_seqs = [
        step["seq"] for step in replay["timeline"] if step["kind"] == "release_decided"
    ]
    explanation = platform.link_explain(decision_seqs[-1])
    assert explanation["decision"] == "auto_granted"
    assert explanation["evidence"]["position_m"] == 3


def test_link_journal_persists_across_restart(tmp_path, clock):
    from psdline.app import build_platform
    from psdline.config import default_config

    first = build_platform(default_config(), clock=clock, storage_root=str(tmp_path / "s"))
    first.start()
    for i in range(1, 4):
        _feed(first, clock, "T01", i, position_m=i)
    events_before = len(first.link_log)

    second = build_platform(default_config(), clock=clock, storage_root=str(tmp_path / "s"))
    assert len(second.link_log) == events_before
    assert second.link_verify()["ok"] is True
    assert second.links.is_adopted("T01") is True
    assert second.link_view("T01")["position_m"] == 3
