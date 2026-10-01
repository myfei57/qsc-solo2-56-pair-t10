"""门控链路：开门、关门、队列、批量与联动。"""

from __future__ import annotations

import pytest

from psdline.door import (
    active_group,
    batch_open,
    close_and_audit,
    close_durable,
    depart,
    depart_policy,
    departed,
    door_report,
    door_state,
    enqueue,
    open_doors,
    open_group,
    open_pressure,
    opened,
    process_queue,
    queued_total,
)
from psdline.errors import InterlockError, MappingError


def test_open_requires_durable_alignment(platform):
    with pytest.raises(InterlockError):
        open_doors(platform.doors, "T01", at=1.0)
    platform.arrive("T01", "ST1", 0)
    assert opened(platform.doors, "T01") is True
    assert platform.doors.opens == 1


def test_close_and_audit_records_and_rolls_back_on_failure(platform):
    class BrokenAudit:
        def record(self, *args, **kwargs):
            raise RuntimeError("audit sink down")

    platform.arrive("T01", "ST1", 0)
    with pytest.raises(RuntimeError):
        close_and_audit(platform.doors, BrokenAudit(), "T01", "ST1", "depart", at=2.0)
    assert close_durable(platform.doors, "T01") is False
    close_and_audit(platform.doors, platform.audit, "T01", "ST1", "depart", at=3.0)
    assert close_durable(platform.doors, "T01") is True


def test_queue_and_depart_clears_commands(platform):
    platform.arrive("T01", "ST1", 0)
    enqueue(platform.doors, "T01", "cmd-1", "open", "ST1")
    enqueue(platform.doors, "T01", "cmd-2", "close", "ST1")
    assert process_queue(platform.doors, "T01") == ["cmd-1", "cmd-2"]
    assert queued_total(platform.doors) == 2
    outcome = depart(platform.doors, "T01", at=9.0)
    assert outcome["cleared_commands"] == 2
    assert departed(platform.doors, "T01") is True
    assert process_queue(platform.doors, "T01") == []
    assert platform.commands.epoch == 1


def test_batch_open_respects_order_and_limit(platform):
    platform.doors.set_batch_limit(2)
    layout = platform.layouts.get("ST1")
    outcome = batch_open(platform.doors, "ST1", list(layout.zones))
    assert outcome["limit"] == 2
    assert len(outcome["opened"]) == 2
    assert outcome["deferred"]
    assert outcome["order"][0] == "G01"
    assert platform.gates.by_station("ST1")[0].open is True


def test_batch_open_skips_locked_groups(platform):
    gate = platform.gates.by_station("ST1")[0]
    platform.set_fault(gate.id)
    outcome = batch_open(platform.doors, "ST1", list(platform.layouts.get("ST1").zones))
    assert outcome["skipped"][gate.group] == "locked"


def test_open_group_uses_mapping(platform):
    linked = open_group(platform.doors, platform.mappings, "ST1", "G02")
    assert linked["group"] == "G02"
    assert active_group(platform.doors, "ST1") == "G02"
    with pytest.raises(MappingError):
        open_group(platform.doors, platform.mappings, "ST1", "NOPE")
    assert active_group(platform.doors, "ST9") == ""


def test_door_state_and_policy(platform):
    platform.arrive("T01", "ST1", 0)
    policy = depart_policy(platform.doors, "T01")
    assert policy["checks"]["opened"] is True
    assert policy["allowed"] is False
    assert "close_durable" in policy["failures"]
    state = door_state(platform.doors, "T01")
    assert state["opened"] is True and state["closed"] is False


def test_door_report_and_pressure(platform):
    platform.arrive("T01", "ST1", 0)
    pressure = open_pressure(platform.doors, "ST1")
    assert pressure["open_count"] >= 1
    report = door_report(platform.doors, ["ST1", "ST2"])
    assert report["opens"] == 1
    assert len(report["stations"]) == 2
