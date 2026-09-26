from datetime import datetime
from unittest.mock import MagicMock

import pytest

from heizungsbruecke.manifest import OPTIONAL_SNAPSHOT_ROLES, SNAPSHOT_ROLES, ChannelManifest
from heizungsbruecke.snapshot import SnapshotRead, publish_snapshot, read_snapshot_roles


def _all_roles_manifest(**extra):
    return ChannelManifest(entity_ids={
        **{role: f"sensor.{role}" for role in SNAPSHOT_ROLES},
        "room_actual": "sensor.room_actual",
        **extra,
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
    manifest = _all_roles_manifest(outdoor_temp="sensor.outdoor_temp", flow_temperature="sensor.flow")
    ha_api = MagicMock()
    ha_api.get_state.return_value = 20.0

    read = read_snapshot_roles(manifest, ha_api)

    assert read == SnapshotRead(roles={role: 20.0 for role in SNAPSHOT_ROLES}, invalid_roles=())
    read_entities = {call.args[0] for call in ha_api.get_state.call_args_list}
    assert read_entities == {f"sensor.{role}" for role in SNAPSHOT_ROLES} | {"sensor.room_actual"}


def test_read_snapshot_roles_marks_unreadable_role_invalid_and_reads_the_others():
    ha_api = MagicMock()
    ha_api.get_state.side_effect = _states_with({"sensor.dat": ValueError("unavailable")})

    read = read_snapshot_roles(_all_roles_manifest(), ha_api)

    assert read.invalid_roles == ("dat",)
    assert set(read.roles) == set(SNAPSHOT_ROLES) - {"dat"}


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_read_snapshot_roles_marks_non_finite_value_invalid(bad):
    ha_api = MagicMock()
    ha_api.get_state.side_effect = _states_with({"sensor.dart": bad})

    assert read_snapshot_roles(_all_roles_manifest(), ha_api).invalid_roles == ("dart",)


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


def test_read_snapshot_roles_includes_valid_optional_and_computed_roles():
    manifest = _all_roles_manifest(outdoor_min_24h="sensor.omin")
    ha_api = MagicMock()
    ha_api.get_state.side_effect = _states_with({"sensor.omin": 12.0})

    read = read_snapshot_roles(manifest, ha_api, computed_values={"room_target_avg_24h": 20.4})

    assert read.roles["outdoor_min_24h"] == 12.0
    assert read.roles["room_target_avg_24h"] == 20.4
    assert set(read.roles) == set(SNAPSHOT_ROLES) | set(OPTIONAL_SNAPSHOT_ROLES)


@pytest.mark.parametrize("computed", [None, float("nan")])
def test_read_snapshot_roles_omits_unusable_optional_roles_silently(computed):
    manifest = _all_roles_manifest(outdoor_min_24h="sensor.omin")
    ha_api = MagicMock()
    ha_api.get_state.side_effect = _states_with({"sensor.omin": ValueError("unknown")})

    read = read_snapshot_roles(manifest, ha_api, computed_values={"room_target_avg_24h": computed})

    assert set(read.roles) == set(SNAPSHOT_ROLES)
    assert read.invalid_roles == ()


def test_publish_snapshot_sends_one_schema_2_message():
    mqtt_client = MagicMock()

    publish_snapshot(mqtt_client, seq="s1", trigger="daily", roles={"dat": 4.0})

    mqtt_client.publish_snapshot.assert_called_once()
    payload = mqtt_client.publish_snapshot.call_args.args[0]
    assert payload["schema"] == 2
    assert payload["seq"] == "s1"
    assert payload["trigger"] == "daily"
    assert payload["roles"] == {"dat": 4.0}
    assert datetime.fromisoformat(payload["ts"]).tzinfo is not None
