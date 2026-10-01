"""间隙探测与运行配额。"""

from __future__ import annotations

import pytest

from psdline.errors import InterlockError, RailError
from psdline.gap import GapRegistry, gap_status, gap_summary
from psdline.quota import QuotaRegistry
from psdline.store import JsonStore


def test_gap_requires_alignment(tmp_path):
    gaps = GapRegistry(JsonStore(str(tmp_path)))
    with pytest.raises(InterlockError):
        gaps.check("T01", at=1.0)
    JsonStore(str(tmp_path)).write("train/T01/align", {"train_id": "T01", "position_m": 0, "durable": True, "at": 0.0})
    result = gaps.check("T01", at=2.0)
    assert result.clear is True and result.confirmed is True
    assert gaps.clear("T01") is True
    assert gaps.checks == 1


def test_gap_probe_marks_blocked(tmp_path):
    gaps = GapRegistry(JsonStore(str(tmp_path)))
    gaps.probe("T01", False, at=1.0)
    assert gaps.state("T01") == "blocked"
    assert gaps.blocked_ids() == ["T01"]
    assert gaps.probe("T01", True, at=2.0).confirmed is True
    assert gaps.blocked_ids() == []
    assert gaps.probes == 2


def test_gap_reports(tmp_path):
    gaps = GapRegistry(JsonStore(str(tmp_path)))
    gaps.probe("T01", True, at=1.0)
    gaps.probe("T02", False, at=1.0)
    assert gap_status(gaps, "T01")["clear"] is True
    summary = gap_summary(gaps, ["T01", "T02"])
    assert summary["count"] == 2 and summary["clear"] == 1
    assert summary["blocked"] == ["T02"]


def test_quota_consume_and_reset(tmp_path):
    quotas = QuotaRegistry(JsonStore(str(tmp_path)))
    quotas.set_limit("arrival", 2)
    assert quotas.consume("arrival") is True
    assert quotas.consume("arrival") is True
    assert quotas.consume("arrival") is False
    assert quotas.remaining("arrival") == 0
    quotas.reset("arrival")
    assert quotas.remaining("arrival") == 2
    assert quotas.status("arrival")["configured"] is True
    assert quotas.status("depart")["configured"] is False
    with pytest.raises(RailError):
        quotas.consume("depart")
    with pytest.raises(RailError):
        quotas.set_limit("arrival", 0)
    assert quotas.reset("depart") is None
