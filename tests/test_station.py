"""站台、门区顺序与布局。"""

from __future__ import annotations

import pytest

from psdline.errors import RailError
from psdline.station import (
    DoorZone,
    Layout,
    Schedule,
    Station,
    nearest_zone,
    open_order,
    spread_m,
    validate_station,
)


def test_open_order_sorts_by_distance():
    zones = [
        DoorZone(id="G03", distance_m=80),
        DoorZone(id="G01", distance_m=10),
        DoorZone(id="G02", distance_m=40),
    ]
    assert open_order(zones) == ["G01", "G02", "G03"]
    assert nearest_zone(zones).id == "G01"
    assert spread_m(zones) == 70


def test_open_order_is_stable_on_ties():
    zones = [DoorZone(id="A", distance_m=5), DoorZone(id="B", distance_m=5)]
    assert open_order(zones) == ["A", "B"]
    assert nearest_zone([]) is None
    assert spread_m([]) == 0


def test_validate_station_rejects_duplicates():
    with pytest.raises(RailError):
        validate_station(Station(id="ST1", name="站台1", line="L1", door_groups=["G01", "G01"]))
    with pytest.raises(RailError):
        validate_station(Station(id="", name="站台1", line="L1", door_groups=["G01"]))
    with pytest.raises(RailError):
        validate_station(Station(id="ST1", name="", line="L1", door_groups=[]))
    validate_station(Station(id="ST1", name="站台1", line="L1", door_groups=["G01"]))


def test_layout_registry_increments_revision(tmp_path):
    from psdline.store import JsonStore
    from psdline.station import LayoutRegistry

    registry = LayoutRegistry(JsonStore(str(tmp_path)))
    layout = registry.save(Layout(station_id="ST1", zones=[DoorZone("G01", 0)]))
    assert layout.revision == 1
    again = registry.save(Layout(station_id="ST1", zones=[DoorZone("G01", 5)]))
    assert again.revision == 2
    assert registry.zone_distance("ST1", "G01") == 5
    with pytest.raises(RailError):
        registry.zone_distance("ST9", "G01")
    with pytest.raises(RailError):
        registry.save(Layout(station_id="", zones=[]))


def test_schedule_registry_minimum_headway(tmp_path):
    from psdline.store import JsonStore
    from psdline.station import ScheduleRegistry

    registry = ScheduleRegistry(JsonStore(str(tmp_path)))
    registry.save(Schedule(station_id="ST1", headways=[180, 120, 240]))
    assert registry.minimum_headway("ST1") == 120
    with pytest.raises(RailError):
        registry.save(Schedule(station_id="ST1", headways=[]))
    with pytest.raises(RailError):
        registry.save(Schedule(station_id="ST1", headways=[0]))


def test_platform_seeds_expected_station_shape(platform):
    stations = platform.stations.list()
    assert [item.id for item in stations] == ["ST1", "ST2", "ST3"]
    assert platform.stations.group_count("ST1") == 4
    assert len(platform.gates.by_station("ST1")) == 24
    view = platform.station_view("ST1")
    assert view["order"][0] == "G01"
    assert view["span_m"] > 0
    assert view["revision"] == 1
