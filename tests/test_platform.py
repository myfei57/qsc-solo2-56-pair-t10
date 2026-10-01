"""平台级到发链路与状态快照。"""

from __future__ import annotations

from psdline.errors import RailError
from psdline.door import departed


def test_start_reports_ready(platform):
    report = platform.start()
    assert report["ready"] is True
    assert report["stations"] == 3
    assert report["gates"] == 72
    assert report["missing"] == {}


def test_arrive_then_depart_full_chain(platform):
    arrival = platform.arrive("T01", "ST1", 0)
    assert arrival["ok"] is True
    assert [step["action"] for step in arrival["steps"]] == ["arrival_signal", "align"]
    assert platform.align.durable("T01") is True
    assert platform.train_doors.is_open("T01") is True
    gap = platform.check_gap("T01")
    assert gap["ok"] is True
    departure = platform.depart("T01", "ST1")
    assert departure["ok"] is True
    assert departure["close_durable"] is True
    assert departure["aspect"] == "green"
    assert departed(platform.doors, "T01") is True


def test_arrive_rejects_out_of_tolerance_and_unknown_station(platform):
    rejected = platform.arrive("T01", "ST1", 99)
    assert rejected["ok"] is False
    assert rejected["violations"]
    assert platform.align.get("T01") is None
    unknown = platform.arrive("T01", "ST9", 0)
    assert unknown["ok"] is False


def test_depart_blocked_by_blocked_gap(platform):
    platform.arrive("T01", "ST1", 0)
    platform.check_gap("T01", clear=False)
    departure = platform.depart("T01", "ST1")
    assert departure["ok"] is False
    assert "间隙探测未确认" in departure["violations"]


def test_renumber_links_new_group(platform):
    result = platform.renumber("ST1", {"A": "G02", "B": "G03"})
    assert result["ok"] is True
    assert result["revision"] == 2
    assert result["active_group"] == "G02"
    assert platform.mappings.revision("ST1") == 2
    assert platform.station_view("ST1")["active_group"] == "G02"


def test_fault_locks_gate_and_clear_releases(platform):
    gate = platform.gates.by_station("ST1")[0]
    fault = platform.set_fault(gate.id)
    assert fault["locked"] is True
    assert platform.lockouts.is_locked(gate.id) is True
    cleared = platform.clear_fault(gate.id)
    assert cleared["ok"] is True
    assert platform.lockouts.is_locked(gate.id) is False


def test_sequence_verdict_handles_rollover(platform):
    rollover = platform.sequence_verdict(65534, 4)
    assert rollover["ok"] is True
    replay = platform.sequence_verdict(10, 0)
    assert replay["ok"] is False
    assert replay["violations"] == ["replay"]


def test_refresh_position_rejects_out_of_range(platform):
    accepted = platform.refresh_position("T01", 12)
    assert accepted["ok"] is True
    assert platform.positions.get("T01") == 12
    rejected = platform.refresh_position("T01", 9999)
    assert rejected["ok"] is False
    assert rejected["violations"]


def test_batch_open_and_sweep(platform):
    batch = platform.batch_open("ST1")
    assert batch["ok"] is True
    assert batch["order"][0] == "G01"
    assert batch["nearest"] == "G01"
    swept = platform.sweep("ST1")
    assert swept["closed"] == 24
    assert platform.gates.count_open() == 0


def test_cycle_reports_interlock_and_queue(platform):
    platform.arrive("T01", "ST1", 0)
    platform.enqueue("T01", "c1", "open", "ST1")
    report = platform.cycle(dt=1.0)
    assert report["ok"] is True
    assert report["cycle"] == 1
    assert report["violations"] == []
    assert "queued_commands" in platform.trend.signals()
    assert platform.cycle(dt=-1)["ok"] is False


def test_process_queue_requires_depart_policy(platform):
    platform.arrive("T01", "ST1", 0)
    blocked = platform.process_queue("T01")
    assert blocked["ok"] is True
    assert blocked["ready"] is False
    assert "close_durable" in blocked["violations"]
    platform.check_gap("T01")
    platform.depart("T01", "ST1")
    allowed = platform.process_queue("T01")
    assert allowed["ready"] is True


def test_status_and_snapshot_cover_every_component(platform):
    platform.arrive("T01", "ST1", 0)
    status = platform.status()
    for key in (
        "namespace",
        "namespaces",
        "stations",
        "trains",
        "doors",
        "door_report",
        "signals",
        "interlock",
        "gaps",
        "quota",
        "store",
        "audit",
        "trend",
    ):
        assert key in status
    snapshot = platform.snapshot()
    assert snapshot["groups"]["ST1"] == ["G01", "G02", "G03", "G04"]
    assert snapshot["adjacent"]
    assert snapshot["timeline"]


def test_misc_operations(platform):
    assert platform.register_dwell("T01", 30.0, "ST1")["ok"] is True
    assert platform.assign_route("T01", "ST1", "ST2", 1500)["ok"] is True
    assert platform.arm("T01", True)["armed"] is True
    assert platform.resolve_group("ST1", "G01")["group"] == "G01"
    platform.arrive("T01", "ST1", 0)
    error = platform.note_error(RailError("boom"))
    assert error["kind"] == "error"
    assert platform.stop()["ok"] is True
    assert platform.reset_state() is True
    assert platform.reset_state() is False
