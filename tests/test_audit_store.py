"""审计流水与文件型持久化。"""

from __future__ import annotations

from psdline.audit import (
    AuditLog,
    build_timeline,
    by_kind,
    by_train,
    count,
    count_by_kind,
    kinds,
    last,
    latest_by_kind,
)
from psdline.clock import ManualClock
from psdline.eventbus import Event, EventBus
from psdline.store import CommandLog, DoorCommand, JsonStore, JsonlJournal, StoreMetrics


def _log(tmp_path) -> tuple[AuditLog, EventBus, ManualClock]:
    store = JsonStore(str(tmp_path))
    bus = EventBus()
    clock = ManualClock()
    return AuditLog(clock, bus, JsonlJournal(store.root, "audit", segment_limit=2)), bus, clock


def test_audit_records_and_replays(tmp_path):
    log, bus, _clock = _log(tmp_path)
    seen: list[Event] = []
    log.record("arrive", "T01", "ST1")
    log.record("depart", "T02", "ST1")
    unsubscribe = bus.subscribe("audit", seen.append)
    log.record("gap_probe", "T01", "ST1", clear=True)
    unsubscribe()
    entries = log.list()
    assert count(entries) == 3
    assert [entry.kind for entry in by_kind(entries, "arrive")] == ["arrive"]
    assert len(by_train(entries, "T01")) == 2
    assert kinds(entries) == ["arrive", "depart", "gap_probe"]
    assert count_by_kind(entries, "arrive") == 1
    assert last(entries).kind == "gap_probe"
    assert latest_by_kind(entries, "arrive").train_id == "T01"
    replay = log.replay()
    assert len(replay) == 3
    assert log.journal.count() == 3
    assert len(log.journal.segments()) == 2
    assert log.summary()["by_kind"]["arrive"] == 1
    assert "arrive" in log.dump()
    assert [event.action for event in seen] == ["gap_probe"]


def test_audit_rejects_empty_kind(tmp_path):
    log, _bus, _clock = _log(tmp_path)
    try:
        log.record("", "", "")
    except Exception as error:  # noqa: BLE001 - 只验证类型
        assert type(error).__name__ == "RailError"
    else:  # pragma: no cover - 不应走到这里
        raise AssertionError("空动作类型必须被拒绝")


def test_audit_timeline_orders_by_time(tmp_path):
    log, _bus, clock = _log(tmp_path)
    log.record("arrive", "T01", "ST1", at=100.0)
    clock.advance(5)
    log.record("depart", "T01", "ST1", at=105.0)
    timeline = build_timeline(log.list(), "T01")
    assert [item["kind"] for item in timeline] == ["arrive", "depart"]
    assert timeline[0]["step"] == 1


def test_json_store_atomic_roundtrip(tmp_path):
    store = JsonStore(str(tmp_path))
    store.write("station/ST1/layout", {"zones": 4})
    assert store.exists("station/ST1/layout") is True
    assert store.read("station/ST1/layout") == {"zones": 4}
    assert store.list_ids() == ["station/ST1/layout"]
    assert store.remove("station/ST1/layout") is True
    assert store.remove("station/ST1/layout") is False
    assert store.read("station/ST1/layout") is None


def test_journal_segments_roll(tmp_path):
    journal = JsonlJournal(str(tmp_path), "audit", segment_limit=2)
    for index in range(5):
        journal.append({"index": index})
    assert journal.count() == 5
    assert len(journal.segments()) == 3
    assert journal.active_segment() == 2
    assert [item["index"] for item in journal.read_segment(0)] == [0, 1]
    assert len(journal.read_all()) == 5
    assert journal.read_segment(9) == []
    assert journal.topic == "audit" and journal.segment_limit == 2


def test_command_log_queue(tmp_path):
    log = CommandLog(JsonStore(str(tmp_path)))
    log.append(DoorCommand(id="c1", train_id="T01", kind="open", epoch=log.epoch))
    log.append(DoorCommand(id="c2", train_id="T01", kind="close", epoch=log.epoch, station_id="ST1"))
    assert log.ids("T01") == ["c1", "c2"]
    assert log.total_queued() == 2
    assert log.pending("T01")[1].station_id == "ST1"
    assert log.clear("T01") == 2
    assert log.epoch == 1
    assert log.pending("T01") == []


def test_store_metrics_summarises(tmp_path):
    store = JsonStore(str(tmp_path))
    store.write("a/one", {"x": 1})
    journal = JsonlJournal(store.root, "audit", segment_limit=10)
    journal.append({"x": 1})
    metrics = StoreMetrics(store.root)
    summary = metrics.summary()
    assert summary["files"] >= 2
    assert summary["audit_segments"] == 1
    assert metrics.usage("a").files == 1
    assert metrics.root == store.root
