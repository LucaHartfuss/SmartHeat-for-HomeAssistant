from datetime import datetime
from unittest.mock import MagicMock

import pytest

from heizungsbruecke.manifest import SNAPSHOT_ROLES, ChannelManifest
from heizungsbruecke.snapshot import SnapshotRead, publish_snapshot, read_snapshot_roles


def _all_roles_manifest():
    return ChannelManifest(entity_ids={
        **{role: f"sensor.{role}" for role in SNAPSHOT_ROLES},
        "room_actual": "sensor.room_actual",
    })


def _states_with(broken: dict):
    """get_state-Ersatz: Entities aus `broken` werfen bzw. liefern den dort hinterlegten
    Wert, alle anderen 20.0."""
    def _get_state(entity_id):
        value = broken.get(entity_id, 20.0)
        if isinstance(value, Exception):
            raise value
        return value
    return _get_state


def test_read_snapshot_roles_reads_required_roles_and_checks_room_actual():
    manifest = _all_roles_manifest()
    ha_api = MagicMock()
    ha_api.get_state.return_value = 20.0

    read = read_snapshot_roles(manifest, ha_api)

    assert read == SnapshotRead(roles={role: 20.0 for role in SNAPSHOT_ROLES}, invalid_roles=())
    read_entities = {call.args[0] for call in ha_api.get_state.call_args_list}
    assert read_entities == {f"sensor.{role}" for role in SNAPSHOT_ROLES} | {"sensor.room_actual"}


def test_read_snapshot_roles_marks_unreadable_role_invalid_and_reads_the_others():
    ha_api = MagicMock()
    ha_api.get_state.side_effect = _states_with({"sensor.heat_limit": ValueError("unavailable")})

    read = read_snapshot_roles(_all_roles_manifest(), ha_api)

    assert read.invalid_roles == ("heat_limit",)
    assert set(read.roles) == set(SNAPSHOT_ROLES) - {"heat_limit"}


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_read_snapshot_roles_marks_non_finite_value_invalid(bad):
    ha_api = MagicMock()
    ha_api.get_state.side_effect = _states_with({"sensor.room_target": bad})

    assert read_snapshot_roles(_all_roles_manifest(), ha_api).invalid_roles == ("room_target",)


def test_read_snapshot_roles_checks_room_actual_but_never_sends_it():
    ha_api = MagicMock()
    ha_api.get_state.side_effect = _states_with({"sensor.room_actual": ValueError("unavailable")})

    read = read_snapshot_roles(_all_roles_manifest(), ha_api)

    assert read.invalid_roles == ("room_actual",)
    assert "room_actual" not in read.roles


def test_read_snapshot_roles_skips_unmapped_roles_without_marking_them_invalid():
    manifest = ChannelManifest(entity_ids={"heat_limit": "number.h"})
    ha_api = MagicMock()
    ha_api.get_state.return_value = 16.0

    assert read_snapshot_roles(manifest, ha_api) == SnapshotRead(roles={"heat_limit": 16.0}, invalid_roles=())


def test_read_snapshot_roles_never_sends_notifications():
    # T2-13: Meldungen kommen nur noch aus der Zustellung (einmal pro Fehlerbeginn).
    ha_api = MagicMock()
    ha_api.get_state.side_effect = ValueError("unavailable")

    read = read_snapshot_roles(_all_roles_manifest(), ha_api)

    assert read.invalid_roles == tuple(SNAPSHOT_ROLES) + ("room_actual",)
    ha_api.send_notification.assert_not_called()


def test_publish_snapshot_sends_one_schema_3_message():
    mqtt_client = MagicMock()

    publish_snapshot(mqtt_client, seq="s1", trigger="daily", roles={"heat_limit": 4.0})

    mqtt_client.publish_snapshot.assert_called_once()
    payload = mqtt_client.publish_snapshot.call_args.args[0]
    assert payload["schema"] == 3
    assert payload["seq"] == "s1"
    assert payload["trigger"] == "daily"
    assert payload["roles"] == {"heat_limit": 4.0}
    assert datetime.fromisoformat(payload["ts"]).tzinfo is not None


def test_snapshot_carries_a_manual_override_only_when_given():
    client = MagicMock()

    publish_snapshot(client, seq="s", trigger="daily", roles={"heat_limit": 1.0})
    assert "manual_override" not in client.publish_snapshot.call_args.args[0]

    publish_snapshot(
        client, seq="s", trigger="daily", roles={"heat_limit": 1.0},
        manual_override={"curve": 1.3, "shift": 24.5, "erkannt": "2026-10-01T08:00:00+02:00", "fremd": 1},
    )
    assert client.publish_snapshot.call_args.args[0]["manual_override"] == {
        "curve": 1.3, "shift": 24.5, "erkannt": "2026-10-01T08:00:00+02:00",
    }


# --- Pflichtrollen aus computed_values (TP11: shift_current kommt von HaPlantBinding.read_or,
# nicht von einem Live-Read der Zone) ---

MANIFEST = ChannelManifest(entity_ids={
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


def test_computed_required_role_is_not_read():
    ha = Ha({"number.hl": 15.0, "sensor.t": 20.5, "number.c": 1.05, "sensor.r": 20.1})
    read = read_snapshot_roles(MANIFEST, ha, computed_values={"shift_current": 21.0})
    assert read.roles["shift_current"] == 21.0
    assert "climate.zone::temperature" not in ha.reads
    assert read.invalid_roles == ()


def test_missing_computed_required_role_is_invalid():
    ha = Ha({"number.hl": 15.0, "sensor.t": 20.5, "number.c": 1.05, "sensor.r": 20.1})
    read = read_snapshot_roles(MANIFEST, ha, computed_values={"shift_current": None})
    assert read.invalid_roles == ("shift_current",)
