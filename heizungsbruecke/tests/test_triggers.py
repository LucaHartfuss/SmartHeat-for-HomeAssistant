"""HA-Trigger- und MQTT-Anbindung (triggers.py): Callbacks stellen nur in den Worker ein."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from heizungsbruecke import triggers
from heizungsbruecke.manifest import ChannelManifest
from heizungsbruecke.runtime import EV_LOCAL_CHECK, EV_SETPOINTS
from heizungsbruecke.worker import Event, RegulationWorker


@pytest.mark.parametrize("raw,retain", [
    (b'{"seq": "s1"}', True), (b"[]", False), (b'"x"', False), (b"{kaputt", False), (b"", False),
])
def test_setpoints_callback_drops_retained_and_non_object_payloads(clock, raw, retain):
    worker = RegulationWorker(clock=clock)
    seen = []
    worker.register(EV_SETPOINTS, seen.append)
    message = MagicMock()
    message.payload = raw
    message.retain = retain

    triggers.make_setpoints_callback(worker)(None, None, message)  # darf nicht werfen
    worker.run_pending()

    assert seen == []


def test_setpoints_callback_posts_fresh_answer_to_worker(clock):
    worker = RegulationWorker(clock=clock)
    seen = []
    worker.register(EV_SETPOINTS, seen.append)
    message = MagicMock()
    message.payload = b'{"seq": "s1", "status": "ok"}'
    message.retain = False

    triggers.make_setpoints_callback(worker)(None, None, message)
    worker.run_pending()

    assert seen == [Event(EV_SETPOINTS, {"payload": {"seq": "s1", "status": "ok"}})]


def test_trigger_callback_coalesces_flood_and_keeps_room_target_flag(clock):
    worker = RegulationWorker(clock=clock)
    seen = []
    worker.register(EV_LOCAL_CHECK, seen.append)
    manifest = ChannelManifest(entity_ids={"room_actual": "sensor.room_actual", "room_target": "sensor.room_target"})
    callback = triggers.make_trigger_event_callback(manifest, worker)

    for _ in range(10):
        callback({"platform": "state", "entity_id": "sensor.room_actual", "attribute": None})
    callback({"platform": "state", "entity_id": "sensor.room_target", "attribute": None})
    callback({"platform": "time"})
    worker.run_pending()

    assert seen == [Event(EV_LOCAL_CHECK, {"room_target_fired": True})]


@pytest.mark.parametrize("trigger,fired", [
    ({"platform": "state", "entity_id": "climate.wz", "attribute": "temperature"}, True),
    ({"platform": "state", "entity_id": "climate.wz", "attribute": "current_temperature"}, False),
    ({"platform": "state", "entity_id": "climate.wz"}, False),
    ({"platform": "time"}, False),
])
def test_trigger_callback_matches_room_target_by_entity_and_attribute(clock, trigger, fired):
    worker = RegulationWorker(clock=clock)
    seen = []
    worker.register(EV_LOCAL_CHECK, seen.append)
    manifest = ChannelManifest(entity_ids={
        "room_actual": "climate.wz::current_temperature", "room_target": "climate.wz::temperature",
    })

    triggers.make_trigger_event_callback(manifest, worker)(trigger)
    worker.run_pending()

    assert seen == [Event(EV_LOCAL_CHECK, {"room_target_fired": fired})]


@pytest.mark.parametrize("room_actual,room_target,expected_room_triggers", [
    (
        "climate.wz::current_temperature", "climate.wz::temperature",
        [
            {"platform": "state", "entity_id": "climate.wz", "attribute": "temperature", "for": {"seconds": 10}},
            {"platform": "state", "entity_id": "climate.wz", "attribute": "current_temperature"},
        ],
    ),
    (
        "sensor.room_actual", "sensor.room_target",
        [
            {"platform": "state", "entity_id": "sensor.room_target", "for": {"seconds": 10}},
            {"platform": "state", "entity_id": "sensor.room_actual"},
        ],
    ),
])
def test_build_ha_trigger_client_sets_attribute_on_both_room_triggers(
    monkeypatch, clock, room_actual, room_target, expected_room_triggers,
):
    created = []
    monkeypatch.setattr(
        "heizungsbruecke.triggers.HaTriggerClient", lambda **kwargs: created.append(kwargs) or SimpleNamespace(**kwargs),
    )
    ha_api = MagicMock(token="tok")
    ha_api.websocket_url.return_value = "ws://x/api/websocket"

    triggers.build_ha_trigger_client(
        ChannelManifest(entity_ids={"room_actual": room_actual, "room_target": room_target}),
        {"daily_trigger_time": "12:00"}, ha_api, RegulationWorker(clock=clock),
    )

    assert created[0]["triggers"] == expected_room_triggers + [{"platform": "time", "at": "12:00"}]


def test_strip_attribute_suffix_removes_climate_attribute_syntax():
    assert triggers._strip_attribute_suffix("climate.wohnzimmer::temperature") == "climate.wohnzimmer"


def test_strip_attribute_suffix_passes_through_plain_entity_id():
    assert triggers._strip_attribute_suffix("sensor.target_rt") == "sensor.target_rt"


def test_extract_attribute_suffix_returns_attribute_name():
    assert triggers._extract_attribute_suffix("climate.wohnzimmer::temperature") == "temperature"


def test_extract_attribute_suffix_returns_none_for_plain_entity_id():
    assert triggers._extract_attribute_suffix("sensor.target_rt") is None
