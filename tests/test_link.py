"""车地链路质量监督：采样、降级、退避、恢复、并发与重放。"""

from __future__ import annotations

import threading
from dataclasses import replace

import pytest

from psdline.app import build_platform
from psdline.clock import ManualClock
from psdline.config import default_config
from psdline.errors import ConfigError
from psdline.link import expected_samples, window_quality
from psdline.link.quality import LinkTuning


# ---- 场景辅助 -----------------------------------------------------------


def _ok(platform, clock, train, n=1):
    """按节拍送 n 个好采样（每拍一个）。"""

    for _ in range(n):
        platform.link_sample(train)
        clock.advance(1)


def _silence(platform, clock, seconds):
    """让时钟前进 seconds 秒并跑一个控制周期（合成欠拍）。"""

    clock.advance(seconds)
    platform.cycle()


def _drive_to(platform, clock, train, target, bound=60):
    """按节拍持续送好采样，直到链路到达目标状态。"""

    for _ in range(bound):
        if platform.link_view(train)["state"] == target:
            return
        clock.advance(1)
        platform.link_sample(train)
    if platform.link_view(train)["state"] != target:
        raise AssertionError(f"未能在 {bound} 拍内到达 {target}")


def _drive_to_degraded(platform, clock, train):
    """6 好 2 丢：窗口质量落在降级区。"""

    _ok(platform, clock, train, 6)
    _silence(platform, clock, 2)
    assert platform.link_view(train)["state"] == "degraded"


def _drive_to_lost(platform, clock, train):
    """3 好 6 丢：静默超过失联判定时间。"""

    _ok(platform, clock, train, 3)
    _silence(platform, clock, 6)
    assert platform.link_view(train)["state"] == "lost"


def _audit_kinds(platform):
    return [entry.kind for entry in platform.audit.list()]


# ---- 窗口评分（纯函数） ---------------------------------------------------


def test_quality_window_scoring():
    tuning = LinkTuning(expected_period_s=1.0, window_s=10.0)
    # 冷启动：覆盖不足一拍按一拍算，第一个好采样就是满分
    assert expected_samples(0.0, tuning) == 1
    assert window_quality(1, 0.0, tuning) == 1.0
    # 窗口随覆盖拉长：3 好 / 5 拍 = 0.6
    assert expected_samples(4.0, tuning) == 5
    assert window_quality(3, 4.0, tuning) == pytest.approx(0.6)
    # 满窗 11 拍；全丢为 0；多发不会抬过 1
    assert expected_samples(10.0, tuning) == 11
    assert window_quality(0, 10.0, tuning) == 0.0
    assert window_quality(20, 10.0, tuning) == 1.0
    # 覆盖超过窗口按窗口截断
    assert expected_samples(999.0, tuning) == 11


# ---- 状态机：降级、失联、恢复迟滞 -----------------------------------------


def test_state_degrades_on_silence(platform, clock):
    _ok(platform, clock, "T01", 3)
    assert platform.link_view("T01")["state"] == "good"
    _silence(platform, clock, 6)
    view = platform.link_view("T01")
    assert view["state"] == "lost"
    assert view["trust"] == "untrusted"
    assert view["backoff_level"] == 1
    kinds = _audit_kinds(platform)
    assert "link_state" in kinds


def test_recovery_requires_sustained_good_samples(platform, clock):
    _drive_to_lost(platform, clock, "T01")
    # 单个好样本不恢复（迟滞）
    clock.advance(1)
    platform.link_sample("T01")
    assert platform.link_view("T01")["trust"] == "untrusted"
    # 连续好样本 + 退避观察期 → 先回降级，不直接回良
    _drive_to(platform, clock, "T01", "degraded")
    assert platform.link_view("T01")["trust"] == "reduced"
    # 窗口被好采样重新填满后才回良
    _drive_to(platform, clock, "T01", "good")
    assert platform.link_view("T01")["trust"] == "trusted"


def test_backoff_escalates_on_repeated_loss(platform, clock):
    _drive_to_lost(platform, clock, "T01")
    assert platform.link_view("T01")["backoff_level"] == 1
    _drive_to(platform, clock, "T01", "degraded")
    _silence(platform, clock, 6)
    view = platform.link_view("T01")
    assert view["state"] == "lost"
    assert view["backoff_level"] == 2
    # 二级退避观察期更长：3 拍好样本不足以脱离失联
    for _ in range(3):
        clock.advance(1)
        platform.link_sample("T01")
    assert platform.link_view("T01")["state"] == "lost"
    _drive_to(platform, clock, "T01", "degraded")
    # 回到良好并稳定一个窗口后，退避级别回落
    _drive_to(platform, clock, "T01", "good")
    _ok(platform, clock, "T01", 12)
    assert platform.link_view("T01")["backoff_level"] == 0
    assert "link_backoff" in _audit_kinds(platform)


# ---- 位置可信度 -----------------------------------------------------------


def test_position_held_when_link_lost(platform, clock):
    accepted = platform.refresh_position("T01", 10)
    assert accepted["ok"] is True
    _drive_to_lost(platform, clock, "T01")
    held = platform.refresh_position("T01", 99)
    assert held["ok"] is False
    assert held["reason"] == "link_lost"
    # 位置保持最后可信值，不再静默冻结
    assert platform.positions.get("T01") == 10
    view = platform.train_view("T01")
    assert view["trust"]["level"] == "untrusted"
    assert "position_held" in _audit_kinds(platform)


def test_position_marked_reduced_when_degraded(platform, clock):
    _drive_to_degraded(platform, clock, "T01")
    outcome = platform.refresh_position("T01", 10)
    assert outcome["ok"] is True
    assert outcome["trust"] == "reduced"
    doc = platform.store.read("train/T01/trust")
    assert doc["level"] == "reduced"
    assert doc["state"] == "degraded"
    assert "position_trust" in _audit_kinds(platform)


def test_missing_trust_doc_self_heals(platform, clock):
    platform.refresh_position("T01", 10)
    assert platform.store.exists("train/T01/trust")
    platform.store.remove("train/T01/trust")
    view = platform.train_view("T01")
    assert view["trust"]["level"] == "trusted"
    assert view["trust"]["source"] == "live"


def test_trust_doc_fallback_when_journal_missing(tmp_path):
    """链路流水缺失但信任标注还在（部分失败）：回退到落盘标注。"""

    from psdline.store import JsonStore

    storage = tmp_path / "state"
    store = JsonStore(str(storage))
    store.write(
        "train/T01/trust",
        {
            "train_id": "T01",
            "level": "reduced",
            "state": "degraded",
            "quality": 0.6,
            "backoff_level": 0,
            "at": 1.0,
        },
    )
    platform = build_platform(
        default_config(), clock=ManualClock(), storage_root=str(storage)
    )
    view = platform.train_view("T01")
    assert view["trust"]["level"] == "reduced"
    assert view["trust"]["source"] == "doc"


# ---- 时序错位：重复、乱序、时钟偏移 ----------------------------------------


def test_duplicate_and_late_seq_are_idempotent(platform, clock):
    first = platform.refresh_position("T01", 10, seq=5)
    assert first["ok"] is True
    duplicate = platform.refresh_position("T01", 20, seq=5)
    assert duplicate["ok"] is False
    assert duplicate["reason"] == "duplicate"
    assert platform.positions.get("T01") == 10
    late = platform.refresh_position("T01", 30, seq=3)
    assert late["ok"] is False
    assert late["reason"] == "late"
    assert platform.positions.get("T01") == 10
    fresh = platform.refresh_position("T01", 40, seq=6)
    assert fresh["ok"] is True
    assert platform.positions.get("T01") == 40
    view = platform.link_view("T01")
    assert view["samples_ignored"] == 2
    assert view["samples_ok"] == 2


def test_clock_skew_flagged_but_counted(platform, clock):
    outcome = platform.refresh_position("T01", 10, sent_at=clock.now() + 100)
    assert outcome["ok"] is True
    samples = [
        record
        for record in platform.link.journal.read_all()
        if record.get("type") == "sample"
    ]
    assert samples[-1]["note"] == "clock_skew"
    assert samples[-1]["counted"] is True
    assert samples[-1]["skew_s"] < 0


# ---- 发车放行降级 ---------------------------------------------------------


def test_depart_requires_confirmation_when_degraded(platform, clock):
    platform.arrive("T01", "ST1", 0)
    platform.check_gap("T01")
    _drive_to_degraded(platform, clock, "T01")
    blocked = platform.depart("T01", "ST1")
    assert blocked["ok"] is False
    assert any("人工确认" in item for item in blocked["violations"])
    # 链路闸门在关门之前：没有任何门控副作用
    assert platform.store.read("door/T01/close") is None
    confirmed = platform.depart("T01", "ST1", operator="op-1", confirm_degraded=True)
    assert confirmed["ok"] is True
    assert confirmed["mode"] == "degraded"
    degraded = [e for e in platform.audit.list() if e.kind == "depart_degraded"]
    assert degraded and degraded[0].detail["operator"] == "op-1"
    assert degraded[0].detail["state"] == "degraded"


def test_depart_refused_when_lost(platform, clock):
    platform.arrive("T01", "ST1", 0)
    platform.check_gap("T01")
    _drive_to_lost(platform, clock, "T01")
    blocked = platform.depart("T01", "ST1")
    assert blocked["ok"] is False
    assert any("退避" in item for item in blocked["violations"])
    assert platform.store.read("door/T01/close") is None
    # 失联不留人工旁路
    still = platform.depart("T01", "ST1", operator="op-1", confirm_degraded=True)
    assert still["ok"] is False
    assert "depart_lost_blocked" in _audit_kinds(platform)


def test_no_session_follows_legacy_path(platform):
    platform.arrive("T01", "ST1", 0)
    platform.check_gap("T01")
    result = platform.depart("T01", "ST1")
    assert result["ok"] is True
    assert result["mode"] == "normal"
    assert result["trust"] == "unevaluated"


# ---- 开门降级 -------------------------------------------------------------


def test_doors_open_require_confirmation_when_degraded(platform, clock):
    platform.arrive("T01", "ST1", 0)
    _drive_to_degraded(platform, clock, "T01")
    rejected = platform.arrive("T01", "ST1", 0)
    assert rejected["ok"] is False
    assert any("人工确认" in item for item in rejected["violations"])
    confirmed = platform.arrive("T01", "ST1", 0, operator="op-1")
    assert confirmed["ok"] is True
    assert "door_open_degraded" in _audit_kinds(platform)


def test_doors_open_forbidden_when_lost(platform, clock):
    _drive_to_lost(platform, clock, "T01")
    rejected = platform.arrive("T01", "ST1", 0)
    assert rejected["ok"] is False
    assert any("禁止开门" in item for item in rejected["violations"])
    assert "door_open_lost_blocked" in _audit_kinds(platform)


# ---- 并发刷新 -------------------------------------------------------------


def test_concurrent_refreshes_serialize(platform):
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def worker(base):
        barrier.wait()
        for i in range(50):
            seq = base + i
            try:
                platform.refresh_position("T01", seq % 200, seq=seq)
            except BaseException as error:  # noqa: BLE001 - 测试收集线程异常
                errors.append(error)

    threads = [threading.Thread(target=worker, args=(k * 1000,)) for k in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    # 采样与位置写入按列车串行：最终位置等于最大序号携带的位置
    assert platform.positions.get("T01") == 7049 % 200
    samples = [
        record
        for record in platform.link.journal.read_all()
        if record.get("type") == "sample"
    ]
    assert len(samples) == 400
    # 重放同一批并发报文（含乱序），判定仍然一致
    second = build_platform(
        default_config(), clock=platform.clock, storage_root=platform.store.root
    )
    assert second.replay_link("T01")["consistent"] is True


# ---- 持久化恢复与重放 -----------------------------------------------------


def test_restore_rebuilds_sessions(tmp_path):
    clock = ManualClock()
    storage = str(tmp_path / "state")
    first = build_platform(default_config(), clock=clock, storage_root=storage)
    first.start()
    _drive_to_lost(first, clock, "T01")
    assert first.link_view("T01")["state"] == "lost"

    second = build_platform(default_config(), clock=clock, storage_root=storage)
    view = second.link_view("T01")
    assert view["state"] == "lost"
    assert view["trust"] == "untrusted"

    # 停机期间继续沉默：恢复后第一次控制周期仍判失联
    before = second.link.journal.count()
    clock.advance(3600)
    second.cycle()
    view = second.link_view("T01")
    assert view["state"] == "lost"
    # 停机欠拍合并补记，不会按拍灌爆流水
    assert second.link.journal.count() - before <= 20
    assert second.replay_link("T01")["consistent"] is True


def test_replay_matches_journaled_decisions(platform, clock):
    _ok(platform, clock, "T01", 3)
    _silence(platform, clock, 6)
    platform.refresh_position("T01", 10)  # 失联中：位置被保持
    _drive_to(platform, clock, "T01", "good")
    replay = platform.replay_link("T01")
    assert replay["consistent"] is True
    assert replay["samples"] > 0
    assert replay["final_state"] == "good"
    transitions = [(item["from"], item["to"]) for item in replay["decisions"]]
    assert ("good", "degraded") in transitions
    assert ("degraded", "lost") in transitions
    assert ("lost", "degraded") in transitions
    # 判定带完整证据：质量、静默、连续好采样、退避级别
    evidence = replay["decisions"][0]
    for key in ("quality", "silence_s", "consecutive_ok", "backoff_level", "at"):
        assert key in evidence


# ---- 控制循环与状态视图 ---------------------------------------------------


def test_cycle_reports_link_summary(platform, clock):
    platform.link_sample("T01")
    clock.advance(1)
    platform.link_sample("T01")
    report = platform.cycle()
    assert report["link"]["sessions"] == 1
    assert report["link"]["by_state"]["good"] == 1
    assert "link" in platform.status()


def test_link_view_and_summary(platform, clock):
    _ok(platform, clock, "T01", 2)
    view = platform.link_view("T01")
    assert view["state"] == "good"
    assert view["trust_doc"] == {"level": "trusted", "source": "live"}
    summary = platform.link_summary()
    assert summary["trains"]["T01"]["samples_ok"] == 2
    assert platform.link_view("T99") is None


# ---- 控制台接口 -----------------------------------------------------------


def test_console_link_endpoints(platform, clock):
    from psdline.console.api import Api

    api = Api(platform)
    status, payload = api.handle("POST", "/api/link/sample", {}, {"train_id": "T01"})
    assert status == 200
    assert payload["state"] == "good"
    status, payload = api.handle(
        "POST", "/api/position", {}, {"train_id": "T01", "position_m": 5, "seq": 1}
    )
    assert status == 200
    assert payload["ok"] is True
    status, payload = api.handle("GET", "/api/link/T01", {}, {})
    assert status == 200
    assert payload["state"] == "good"
    status, payload = api.handle("GET", "/api/link/T01/replay", {}, {})
    assert status == 200
    assert payload["consistent"] is True
    status, _payload = api.handle("GET", "/api/link/T99", {}, {})
    assert status == 400
    status, payload = api.handle("GET", "/api/link", {}, {})
    assert status == 200
    assert payload["sessions"] == 1


# ---- 配置校验 -------------------------------------------------------------


def test_link_config_validates():
    base = default_config()
    with pytest.raises(ConfigError):
        replace(base, link=replace(base.link, good_threshold=0.4)).validate()
    with pytest.raises(ConfigError):
        replace(base, link=replace(base.link, lost_after_s=0.5)).validate()
    with pytest.raises(ConfigError):
        replace(base, link=replace(base.link, backoff_max_s=1.0)).validate()
    with pytest.raises(ConfigError):
        replace(base, link=replace(base.link, recover_samples=0)).validate()
