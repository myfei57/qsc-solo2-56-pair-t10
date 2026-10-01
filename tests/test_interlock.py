"""联锁：故障锁定、解除条件与序号回绕。"""

from __future__ import annotations

import pytest

from psdline.errors import RailError
from psdline.interlock import (
    ACTIVE,
    CLEAR,
    FaultRegistry,
    InterlockArmer,
    MODULUS,
    interlock_status,
    next_sequence,
    release_ready,
    releasable,
    restore_lockouts,
    verdict_of,
)
from psdline.psd import LockoutRegistry, PSD, PSDRegistry
from psdline.store import JsonStore


def _gates(tmp_path) -> tuple[PSDRegistry, LockoutRegistry]:
    gates = PSDRegistry(JsonStore(str(tmp_path)))
    gates.upsert(PSD(id="G1", station="ST1", group="G01"))
    gates.upsert(PSD(id="G2", station="ST1", group="G02"))
    return gates, LockoutRegistry(gates)


def test_fault_sets_and_clears_lockout(tmp_path):
    gates, lockouts = _gates(tmp_path)
    faults = FaultRegistry(JsonStore(str(tmp_path)))
    faults.set_fault(lockouts, "G1", at=1.0)
    assert faults.state("G1") == ACTIVE
    assert lockouts.is_locked("G1") is True
    assert faults.active_ids() == ["G1"]
    faults.clear_fault(lockouts, "G1", at=2.0)
    assert faults.state("G1") == CLEAR
    assert lockouts.is_locked("G1") is False
    assert faults.sets == 1 and faults.clears == 1
    with pytest.raises(RailError):
        faults.clear_fault(lockouts, "G9", at=3.0)


def test_releasable_requires_closed_gate(tmp_path):
    gates, _lockouts = _gates(tmp_path)
    gates.set_open("G1", True)
    allowed, reason = releasable(gates, "G1", gap_clear=True)
    assert allowed is False and reason == "gate_open"
    gates.set_open("G1", False)
    allowed, reason = releasable(gates, "G1", gap_clear=False)
    assert allowed is False and reason == "gap_blocked"
    report = release_ready(gates, ["G1"], gap_clear=True)
    assert report["ready"] is True and report["checked"] == 1
    assert releasable(gates, "missing", True)[1] == "gate_missing"


def test_restore_lockouts_follows_fault_table(tmp_path):
    gates, lockouts = _gates(tmp_path)
    faults = FaultRegistry(JsonStore(str(tmp_path)))
    faults.set_fault(lockouts, "G1", at=1.0)
    lockouts.clear("G2")
    report = restore_lockouts(faults, gates, lockouts, gap_clear=True)
    assert report["locked"] == 1
    assert report["released"] == 1
    assert lockouts.is_locked("G1") is True
    assert lockouts.locked_count() == 1
    assert lockouts.changes >= 3


def test_verdict_detects_rollover_and_replay():
    assert next_sequence(65534, 4) == (65534 + 4) % MODULUS
    forward = verdict_of(65534, 2)
    assert forward.advanced is True and forward.reason == "advanced"
    assert forward.delta == 4
    replay = verdict_of(10, 10)
    assert replay.advanced is False and replay.reason == "replay"
    backwards = verdict_of(100, 5)
    assert backwards.advanced is False and backwards.reason == "jump_backwards"
    assert "advanced" in forward.as_dict()


def test_interlock_status_reports_ratio(tmp_path):
    gates, lockouts = _gates(tmp_path)
    faults = FaultRegistry(JsonStore(str(tmp_path)))
    faults.set_fault(lockouts, "G1", at=1.0)
    report = interlock_status(faults, gates, max_locked_ratio=0.5)
    assert report["locked_count"] == 1
    assert report["can_release_depart"] is True
    faults.set_fault(lockouts, "G2", at=2.0)
    report = interlock_status(faults, gates, max_locked_ratio=0.5)
    assert report["can_release_depart"] is False


def test_armer_tracks_trains(tmp_path):
    armer = InterlockArmer(JsonStore(str(tmp_path)))
    assert armer.arm("T01", True) is True
    assert armer.armed("T01") is True
    assert armer.armed_trains() == ["T01"]
    assert armer.arm("T01", False) is False
    assert armer.armed("T01") is False
    assert (armer.arms, armer.disarms) == (1, 1)
