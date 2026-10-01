"""列车对位、车门、停站与交路。"""

from __future__ import annotations

import pytest

from psdline.errors import AlignmentError, RailError
from psdline.store import JsonStore
from psdline.train import (
    AlignRegistry,
    DoorStateRegistry,
    Dwell,
    DwellRegistry,
    PositionTracker,
    Route,
    RouteRegistry,
    Train,
    TrainRegistry,
    train_state,
)


def test_align_requires_tolerance(tmp_path):
    registry = AlignRegistry(JsonStore(str(tmp_path)), tolerance_m=0.5)
    record = registry.align("T01", 10, 10, at=100.0)
    assert record.durable is True
    assert registry.durable("T01") is True
    with pytest.raises(AlignmentError):
        registry.align("T01", 12, 10, at=101.0)
    assert registry.get("T01").position_m == 10


def test_align_rejects_empty_train(tmp_path):
    registry = AlignRegistry(JsonStore(str(tmp_path)))
    with pytest.raises(AlignmentError):
        registry.align("", 0, 0, at=0.0)
    assert registry.clear("T01") is False


def test_train_registry_assign_and_list(tmp_path):
    registry = TrainRegistry(JsonStore(str(tmp_path)))
    registry.upsert(Train(id="T01", line="L1"))
    registry.upsert(Train(id="T02", line="L1"))
    registry.assign_station("T01", "ST2")
    assert [item.id for item in registry.list()] == ["T01", "T02"]
    assert registry.get("T01").station == "ST2"
    assert registry.count() == 2
    with pytest.raises(RailError):
        registry.require("T99")
    with pytest.raises(RailError):
        registry.upsert(Train(id=""))


def test_door_state_and_train_state(tmp_path):
    store = JsonStore(str(tmp_path))
    align = AlignRegistry(store)
    doors = DoorStateRegistry(store)
    positions = PositionTracker(store, max_position_m=100)
    align.align("T01", 5, 5, at=1.0)
    doors.confirm_open("T01", at=2.0)
    positions.update("T01", 6, at=3.0)
    state = train_state("T01", align, doors, positions)
    assert state == {
        "train_id": "T01",
        "aligned": True,
        "alignment_m": 5,
        "doors_open": True,
        "position_m": 6,
    }


def test_position_tracker_rejects_out_of_range(tmp_path):
    tracker = PositionTracker(JsonStore(str(tmp_path)), max_position_m=50, stale_after_s=5.0)
    assert tracker.update("T01", 10, at=100.0)["accepted"] is True
    assert tracker.update("T01", 999, at=101.0)["accepted"] is False
    assert tracker.get("T01") == 10
    assert tracker.stale("T01", 110.0) is True
    assert tracker.stale("T01", 102.0) is False
    assert tracker.update("", 1, at=0.0)["accepted"] is False


def test_dwell_registry_limits(tmp_path):
    registry = DwellRegistry(JsonStore(str(tmp_path)), max_seconds=60)
    registry.set(Dwell(train_id="T01", seconds=30, station_id="ST1"))
    assert registry.get("T01").seconds == 30
    assert registry.remaining("T01", 10) == 20
    assert registry.remaining("T01", 100) == 0
    assert registry.remaining("T99", 0) == 0
    with pytest.raises(RailError):
        registry.set(Dwell(train_id="T01", seconds=-1))
    with pytest.raises(RailError):
        registry.set(Dwell(train_id="T01", seconds=61))
    with pytest.raises(RailError):
        registry.set(Dwell(train_id="", seconds=1))


def test_route_registry_validates(tmp_path):
    registry = RouteRegistry(JsonStore(str(tmp_path)))
    registry.assign(Route(train_id="T01", origin="ST1", destination="ST2", distance_m=1200))
    assert registry.get("T01").destination == "ST2"
    with pytest.raises(RailError):
        registry.assign(Route(train_id="T01", origin="ST1", destination="ST1", distance_m=10))
    with pytest.raises(RailError):
        registry.assign(Route(train_id="T01", origin="ST1", destination="ST2", distance_m=0))
