from datetime import datetime
from unittest.mock import MagicMock

import pytest

from heizungsbruecke.ha_binding import HaPlantBinding
from smartheat_runtime.roles import ChannelManifest
from smartheat_runtime.snapshot import SnapshotRead, publish_snapshot, read_snapshot

LEVERS = ("curve", "room_setpoint", "heat_limit")
# Rollen der Manifest-Entities je Hebel (Add-on-Optionen unveraendert, P2-3).
LEVER_ENTITIES = {"curve": "sensor.curve_current", "room_setpoint": "sensor.shift_current",
                  "heat_limit": "sensor.heat_limit"}


def _all_roles_manifest():
    return ChannelManifest(refs={
        **{role: f"sensor.{role}" for role in ("heat_limit", "room_target", "curve_current", "shift_current")},
        "room_actual": "sensor.room_actual",
    })


def _read(manifest, ha_api, known=None):
    return read_snapshot(manifest, ha_api, HaPlantBinding(ha_api, manifest), known or {})


def _states_with(broken: dict):
    """get_state-Ersatz: Entities aus `broken` werfen bzw. liefern den dort hinterlegten
    Wert, alle anderen 20.0."""
    def _get_state(entity_id):
        value = broken.get(entity_id, 20.0)
        if isinstance(value, Exception):
            raise value
        return value
    return _get_state


def test_read_snapshot_reads_levers_and_room_target_and_checks_room_actual():
    manifest = _all_roles_manifest()
    ha_api = MagicMock()
    ha_api.get_state.return_value = 20.0

    read = _read(manifest, ha_api)

    assert read == SnapshotRead(room_target=20.0, levers={lever: 20.0 for lever in LEVERS}, invalid=())
    read_entities = {call.args[0] for call in ha_api.get_state.call_args_list}
    assert read_entities == set(LEVER_ENTITIES.values()) | {"sensor.room_target", "sensor.room_actual"}


def test_read_snapshot_marks_unreadable_lever_invalid_and_reads_the_others():
    ha_api = MagicMock()
    ha_api.get_state.side_effect = _states_with({"sensor.heat_limit": ValueError("unavailable")})

    read = _read(_all_roles_manifest(), ha_api)

    assert read.invalid == ("heat_limit",)
    assert set(read.levers) == set(LEVERS) - {"heat_limit"}
    assert read.room_target == 20.0


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_read_snapshot_marks_non_finite_value_invalid(bad):
    ha_api = MagicMock()
    ha_api.get_state.side_effect = _states_with({"sensor.room_target": bad})

    read = _read(_all_roles_manifest(), ha_api)

    assert read.invalid == ("room_target",)
    assert read.room_target is None


def test_read_snapshot_checks_room_actual_but_never_sends_it():
    ha_api = MagicMock()
    ha_api.get_state.side_effect = _states_with({"sensor.room_actual": ValueError("unavailable")})

    read = _read(_all_roles_manifest(), ha_api)

    assert read.invalid == ("room_actual",)
    assert "room_actual" not in read.levers


def test_read_snapshot_skips_unmapped_levers_without_marking_them_invalid():
    manifest = ChannelManifest(refs={"heat_limit": "number.h", "room_target": "sensor.t"})
    ha_api = MagicMock()
    ha_api.get_state.return_value = 16.0

    assert _read(manifest, ha_api) == SnapshotRead(room_target=16.0, levers={"heat_limit": 16.0}, invalid=())


def test_read_snapshot_never_sends_notifications():
    # T2-13: Meldungen kommen nur noch aus der Zustellung (einmal pro Fehlerbeginn).
    ha_api = MagicMock()
    ha_api.get_state.side_effect = ValueError("unavailable")

    read = _read(_all_roles_manifest(), ha_api)

    assert read.invalid == LEVERS + ("room_target", "room_actual")
    ha_api.send_notification.assert_not_called()


def test_publish_snapshot_sends_one_schema_4_message():
    mqtt_client = MagicMock()

    publish_snapshot(mqtt_client, seq="s1", trigger="daily", room_target=20.5, levers={"heat_limit": 4.0})

    mqtt_client.publish_snapshot.assert_called_once()
    payload = mqtt_client.publish_snapshot.call_args.args[0]
    assert payload["schema"] == 4
    assert payload["seq"] == "s1"
    assert payload["trigger"] == "daily"
    assert payload["room_target"] == 20.5
    assert payload["levers"] == {"heat_limit": 4.0}
    assert datetime.fromisoformat(payload["ts"]).tzinfo is not None


def test_publish_snapshot_schema_4():
    fake_mqtt = MagicMock()
    publish_snapshot(fake_mqtt, seq="s", trigger="daily", room_target=20.5,
                     levers={"curve": 1.0, "room_setpoint": 17.5, "heat_limit": 16.0},
                     manual_override={"levers": {"curve": 1.2}, "erkannt": "2026-10-03T09:00:00+02:00", "rollen": {}})
    payload = fake_mqtt.publish_snapshot.call_args.args[0]
    assert payload["schema"] == 4
    assert (payload["room_target"], payload["readonly"]) == (20.5, [])
    assert payload["levers"] == {"curve": 1.0, "room_setpoint": 17.5, "heat_limit": 16.0}
    assert payload["manual_override"] == {"levers": {"curve": 1.2}, "erkannt": "2026-10-03T09:00:00+02:00"}


def test_snapshot_carries_a_manual_override_only_when_given():
    client = MagicMock()

    publish_snapshot(client, seq="s", trigger="daily", room_target=20.0, levers={"heat_limit": 1.0})
    assert "manual_override" not in client.publish_snapshot.call_args.args[0]

    publish_snapshot(
        client, seq="s", trigger="daily", room_target=20.0, levers={"heat_limit": 1.0},
        manual_override={"levers": {"curve": 1.3, "room_setpoint": 24.5}, "erkannt": "2026-10-01T08:00:00+02:00",
                         "fremd": 1},
    )
    assert client.publish_snapshot.call_args.args[0]["manual_override"] == {
        "levers": {"curve": 1.3, "room_setpoint": 24.5}, "erkannt": "2026-10-01T08:00:00+02:00",
    }


# --- Hebel aus `known` (TP11: room_setpoint kommt von HaPlantBinding.read_or, nicht von einem Live-Read der
# Zone) ---

MANIFEST = ChannelManifest(refs={
    "heat_limit": "number.hl", "room_target": "sensor.t", "curve_current": "number.c",
    "shift_current": "climate.zone::temperature", "room_actual": "sensor.r",
})


class Ha:
    def __init__(self, states):
        self.states = states
        self.reads = []

    def get_state(self, ref):
        self.reads.append(ref)
        return self.states[ref]


def test_known_lever_is_not_read():
    ha = Ha({"number.hl": 15.0, "sensor.t": 20.5, "number.c": 1.05, "sensor.r": 20.1})
    read = _read(MANIFEST, ha, known={"room_setpoint": 21.0})
    assert read.levers["room_setpoint"] == 21.0
    assert "climate.zone::temperature" not in ha.reads
    assert read.invalid == ()


def test_missing_known_lever_is_invalid():
    ha = Ha({"number.hl": 15.0, "sensor.t": 20.5, "number.c": 1.05, "sensor.r": 20.1})
    read = _read(MANIFEST, ha, known={"room_setpoint": None})
    assert read.invalid == ("room_setpoint",)


# --- Plan 3b: nur lesbare Hebel (Weishaupt-Basis) ---

def _basis_manifest(**extra):
    return ChannelManifest(refs={
        "shift_current": "sensor.shift_current", "room_target": "sensor.room_target", **extra,
    })


def _basis_read(manifest, ha_api):
    from smartheat_core.binding import WEISHAUPT_MODBUS_BASIS

    return read_snapshot(manifest, ha_api, HaPlantBinding(ha_api, manifest, WEISHAUPT_MODBUS_BASIS), {})


def test_basis_reports_the_mapped_curve_as_readonly():
    ha_api = MagicMock()
    ha_api.get_state.side_effect = _states_with({"sensor.curve_current": 0.75})

    read = _basis_read(_basis_manifest(curve_current="sensor.curve_current", heat_limit="sensor.heat_limit"), ha_api)

    assert read.levers == {"room_setpoint": 20.0, "curve": 0.75}
    assert read.readonly == ("curve",)  # die Heizgrenze wird nie gemeldet (Plausibilitaet des Servers 5-25)
    assert read.invalid == ()


def test_basis_without_curve_mapping_reports_nothing_readonly():
    ha_api = MagicMock()
    ha_api.get_state.return_value = 20.0
    read = _basis_read(_basis_manifest(), ha_api)
    assert (read.levers, read.readonly) == ({"room_setpoint": 20.0}, ())


@pytest.mark.parametrize("bad", [ValueError("unavailable"), float("nan")])
def test_an_unreadable_readonly_lever_is_left_out_without_a_data_fault(bad):
    ha_api = MagicMock()
    ha_api.get_state.side_effect = _states_with({"sensor.curve_current": bad})
    read = _basis_read(_basis_manifest(curve_current="sensor.curve_current"), ha_api)
    assert (read.levers, read.readonly, read.invalid) == ({"room_setpoint": 20.0}, (), ())


def test_publish_snapshot_carries_readonly():
    mqtt = MagicMock()
    publish_snapshot(mqtt, "s1", "daily", 21.0, {"room_setpoint": 20.0, "curve": 0.75}, readonly=("curve",))
    payload = mqtt.publish_snapshot.call_args.args[0]
    assert payload["readonly"] == ["curve"]
    assert payload["levers"] == {"room_setpoint": 20.0, "curve": 0.75}
