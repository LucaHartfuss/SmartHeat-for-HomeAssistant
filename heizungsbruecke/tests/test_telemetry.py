from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from smartheat_runtime import telemetry
from smartheat_runtime.delivery import DataFault
from smartheat_runtime.roles import ChannelManifest


def test_publish_telemetry_publishes_payload():
    mqtt_client = MagicMock()

    telemetry.publish_telemetry(mqtt_client=mqtt_client, room_actual=20.5, boost_active=False, failsafe_active=False)

    mqtt_client.publish_telemetry.assert_called_once()
    payload = mqtt_client.publish_telemetry.call_args.args[0]
    assert payload["room_actual"] == 20.5
    assert payload["boost_active"] is False
    assert payload["failsafe_active"] is False
    assert "ts" in payload


def test_run_telemetry_tick_reads_room_actual_and_publishes():
    manifest = ChannelManifest(refs={"room_actual": "sensor.room_actual"})
    ha_api = MagicMock()
    ha_api.get_state.return_value = 20.5
    mqtt_client = MagicMock()

    telemetry.run_telemetry_tick(
        manifest, ha_api, mqtt_client, boost_active=True, failsafe_active=False,
    )

    ha_api.get_state.assert_called_once_with("sensor.room_actual")
    mqtt_client.publish_telemetry.assert_called_once()
    payload = mqtt_client.publish_telemetry.call_args.args[0]
    assert payload["room_actual"] == 20.5
    assert payload["boost_active"] is True
    assert payload["failsafe_active"] is False


def test_run_telemetry_tick_includes_configured_optional_kpi_fields():
    manifest = ChannelManifest(refs={
        "room_actual": "sensor.room_actual",
        "flow_temperature": "sensor.flow",
        "operating_mode": "sensor.mode",
    })
    ha_api = MagicMock()
    ha_api.get_state.side_effect = lambda entity_id: {
        "sensor.room_actual": 20.5, "sensor.flow": 45.2,
    }[entity_id]
    ha_api.get_raw_state.return_value = "heating"
    mqtt_client = MagicMock()

    telemetry.run_telemetry_tick(
        manifest, ha_api, mqtt_client, boost_active=False, failsafe_active=False,
    )

    payload = mqtt_client.publish_telemetry.call_args.args[0]
    assert payload["flow_temperature"] == 45.2
    assert payload["operating_mode"] == "heating"
    ha_api.get_raw_state.assert_called_once_with("sensor.mode")


def test_run_telemetry_tick_omits_unconfigured_optional_kpi_fields():
    manifest = ChannelManifest(refs={"room_actual": "sensor.room_actual"})
    ha_api = MagicMock()
    ha_api.get_state.return_value = 20.5
    mqtt_client = MagicMock()

    telemetry.run_telemetry_tick(
        manifest, ha_api, mqtt_client, boost_active=False, failsafe_active=False,
    )

    payload = mqtt_client.publish_telemetry.call_args.args[0]
    assert "flow_temperature" not in payload
    assert "operating_mode" not in payload
    assert "energy" not in payload
    ha_api.get_raw_state.assert_not_called()


def test_electrical_total_is_read_as_energy_channel():
    manifest = ChannelManifest(refs={"energy_electrical_total": "sensor.e_total"})
    ha_api = MagicMock()
    ha_api.get_state.return_value = 12.5

    fields = telemetry.read_kpi_fields(manifest, ha_api)

    assert fields["energy"] == {"electrical_total": 12.5}


def test_run_telemetry_tick_builds_energy_subobject_from_configured_channels():
    manifest = ChannelManifest(refs={
        "room_actual": "sensor.room_actual",
        "energy_thermal_heating": "sensor.e_thermal",
        "energy_electrical_heating": "sensor.e_elec",
    })
    ha_api = MagicMock()
    ha_api.get_state.side_effect = lambda entity_id: {
        "sensor.room_actual": 20.5, "sensor.e_thermal": 1234.5, "sensor.e_elec": 300.1,
    }[entity_id]
    mqtt_client = MagicMock()

    telemetry.run_telemetry_tick(
        manifest, ha_api, mqtt_client, boost_active=False, failsafe_active=False,
    )

    payload = mqtt_client.publish_telemetry.call_args.args[0]
    assert payload["energy"] == {"thermal_heating": 1234.5, "electrical_heating": 300.1}


def test_run_telemetry_tick_omits_failing_optional_sensor_but_publishes_rest():
    manifest = ChannelManifest(refs={
        "room_actual": "sensor.room_actual",
        "flow_temperature": "sensor.flow",
        "return_temperature": "sensor.ret",
        "operating_mode": "sensor.mode",
    })
    ha_api = MagicMock()

    def fake_get_state(entity_id):
        if entity_id == "sensor.flow":
            raise ValueError("could not convert string to float: 'unavailable'")
        return {"sensor.room_actual": 20.5, "sensor.ret": 30.0}[entity_id]

    ha_api.get_state.side_effect = fake_get_state
    ha_api.get_raw_state.return_value = "heating"
    mqtt_client = MagicMock()

    telemetry.run_telemetry_tick(
        manifest, ha_api, mqtt_client, boost_active=True, failsafe_active=False,
    )

    payload = mqtt_client.publish_telemetry.call_args.args[0]
    assert payload["room_actual"] == 20.5
    assert payload["boost_active"] is True
    assert payload["return_temperature"] == 30.0
    assert payload["operating_mode"] == "heating"
    assert "flow_temperature" not in payload


def test_run_telemetry_tick_omits_operating_mode_when_sensor_unavailable():
    manifest = ChannelManifest(refs={
        "room_actual": "sensor.room_actual",
        "operating_mode": "sensor.mode",
    })
    ha_api = MagicMock()
    ha_api.get_state.return_value = 20.5
    ha_api.get_raw_state.side_effect = ValueError("unavailable")
    mqtt_client = MagicMock()

    telemetry.run_telemetry_tick(
        manifest, ha_api, mqtt_client, boost_active=False, failsafe_active=False,
    )

    payload = mqtt_client.publish_telemetry.call_args.args[0]
    assert payload["room_actual"] == 20.5
    assert "operating_mode" not in payload


def test_run_telemetry_tick_omits_non_finite_kpi_values():
    manifest = ChannelManifest(refs={
        "room_actual": "sensor.room_actual",
        "flow_temperature": "sensor.flow",
        "return_temperature": "sensor.ret",
        "energy_thermal_heating": "sensor.e1",
        "energy_thermal_dhw": "sensor.e2",
    })
    ha_api = MagicMock()
    ha_api.get_state.side_effect = lambda entity_id: {
        "sensor.room_actual": 20.5, "sensor.flow": float("nan"), "sensor.ret": 30.0,
        "sensor.e1": float("inf"), "sensor.e2": 12.0,
    }[entity_id]
    mqtt_client = MagicMock()

    telemetry.run_telemetry_tick(
        manifest, ha_api, mqtt_client, boost_active=False, failsafe_active=False,
    )

    payload = mqtt_client.publish_telemetry.call_args.args[0]
    assert payload["room_actual"] == 20.5
    assert "flow_temperature" not in payload
    assert payload["return_temperature"] == 30.0
    assert payload["energy"] == {"thermal_dhw": 12.0}


def test_run_telemetry_tick_omits_energy_key_when_all_channels_fail():
    manifest = ChannelManifest(refs={
        "room_actual": "sensor.room_actual",
        "energy_thermal_heating": "sensor.e1",
        "energy_thermal_dhw": "sensor.e2",
    })
    ha_api = MagicMock()

    def fake_get_state(entity_id):
        if entity_id == "sensor.room_actual":
            return 20.5
        raise ValueError("unavailable")

    ha_api.get_state.side_effect = fake_get_state
    mqtt_client = MagicMock()

    telemetry.run_telemetry_tick(
        manifest, ha_api, mqtt_client, boost_active=False, failsafe_active=False,
    )

    payload = mqtt_client.publish_telemetry.call_args.args[0]
    assert payload["room_actual"] == 20.5
    assert "energy" not in payload


def test_run_telemetry_tick_skips_when_room_actual_not_mapped():
    manifest = ChannelManifest(refs={})
    ha_api = MagicMock()
    mqtt_client = MagicMock()

    telemetry.run_telemetry_tick(
        manifest, ha_api, mqtt_client, boost_active=False, failsafe_active=False,
    )

    ha_api.get_state.assert_not_called()
    mqtt_client.publish_telemetry.assert_not_called()


def test_publish_telemetry_includes_datenfehler_when_given():
    mqtt_client = MagicMock()

    telemetry.publish_telemetry(
        mqtt_client=mqtt_client, room_actual=20.5, boost_active=False, failsafe_active=False,
        datenfehler=DataFault("write", ("curve", "number.x", "Timeout")),
    )

    payload = mqtt_client.publish_telemetry.call_args.args[0]
    assert payload["datenfehler"] == {"source": "write", "detail": ["curve", "number.x", "Timeout"]}


def test_publish_telemetry_omits_datenfehler_without_fault():
    mqtt_client = MagicMock()

    telemetry.publish_telemetry(mqtt_client=mqtt_client, room_actual=20.5, boost_active=False, failsafe_active=False)

    assert telemetry.DATENFEHLER_KEY not in mqtt_client.publish_telemetry.call_args.args[0]


def test_run_telemetry_tick_passes_datenfehler_through():
    manifest = ChannelManifest(refs={"room_actual": "sensor.room_actual"})
    ha_api = MagicMock()
    ha_api.get_state.return_value = 20.5
    mqtt_client = MagicMock()

    telemetry.run_telemetry_tick(
        manifest, ha_api, mqtt_client, boost_active=False, failsafe_active=False,
        datenfehler=DataFault("local", ("dat",)),
    )

    assert mqtt_client.publish_telemetry.call_args.args[0]["datenfehler"] == {"source": "local", "detail": ["dat"]}


def test_run_telemetry_tick_survives_exception_without_propagating(monkeypatch, caplog):
    manifest = ChannelManifest(refs={"room_actual": "sensor.room_actual"})
    ha_api = MagicMock()
    ha_api.get_state.side_effect = OSError("Datentraeger voll")
    mqtt_client = MagicMock()

    with caplog.at_level("ERROR"):
        telemetry.run_telemetry_tick(
            manifest, ha_api, mqtt_client, boost_active=False, failsafe_active=False,
        )  # must not raise

    assert "Telemetrie" in caplog.text


def test_regulation_fields_and_local_ts():
    manifest = SimpleNamespace(refs={
        "room_actual": "sensor.r", "outdoor_temp": "sensor.o", "flow_setpoint": "sensor.vl",
    })
    ha = MagicMock()
    ha.get_state.side_effect = {"sensor.r": 20.1, "sensor.o": 3.5, "sensor.vl": 41.0}.__getitem__
    mqtt = MagicMock()
    telemetry.run_telemetry_tick(manifest, ha, mqtt, boost_active=False, failsafe_active=False, room_target=20.5)
    payload = mqtt.publish_telemetry.call_args.args[0]
    assert (payload["room_target"], payload["outdoor_temp"], payload["flow_setpoint"]) == (20.5, 3.5, 41.0)
    assert datetime.fromisoformat(payload["ts"]).tzinfo is not None


def test_unreadable_regulation_field_is_omitted():
    manifest = SimpleNamespace(refs={"room_actual": "sensor.r", "outdoor_temp": "sensor.o"})
    ha = MagicMock()

    def _get(ref):
        if ref == "sensor.o":
            raise RuntimeError("unavailable")
        return 20.1
    ha.get_state.side_effect = _get
    mqtt = MagicMock()
    telemetry.run_telemetry_tick(manifest, ha, mqtt, boost_active=False, failsafe_active=False, room_target=None)
    payload = mqtt.publish_telemetry.call_args.args[0]
    assert "outdoor_temp" not in payload and "room_target" not in payload


def test_regulation_fields_constant():
    assert telemetry.REGULATION_FIELDS == ("room_target", "outdoor_temp", "flow_setpoint")


def test_the_payload_always_carries_waerme_fehlt():
    mqtt_client = MagicMock()
    telemetry.publish_telemetry(mqtt_client=mqtt_client, room_actual=20.5, boost_active=False, failsafe_active=False)
    assert mqtt_client.publish_telemetry.call_args.args[0][telemetry.WAERME_FEHLT_KEY] is False
    telemetry.publish_telemetry(
        mqtt_client=mqtt_client, room_actual=20.5, boost_active=False, failsafe_active=False, waerme_fehlt=True,
    )
    assert mqtt_client.publish_telemetry.call_args.args[0]["waerme_fehlt"] is True


def test_run_telemetry_tick_hands_the_readings_to_the_waerme_callback():
    manifest = ChannelManifest(refs={
        "room_actual": "sensor.room", "flow_temperature": "sensor.flow", "flow_setpoint": "sensor.set",
    })
    ha_api = MagicMock()
    readings = {"sensor.room": 21.0, "sensor.flow": 26.0, "sensor.set": 36.0}
    ha_api.get_state.side_effect = lambda entity_id: readings[entity_id]
    mqtt_client = MagicMock()
    seen = []

    def waerme(room, kpi, regulation):
        seen.append((room, kpi["flow_temperature"], regulation["flow_setpoint"]))
        return True

    telemetry.run_telemetry_tick(
        manifest, ha_api, mqtt_client, boost_active=False, failsafe_active=False, waerme=waerme,
    )

    assert seen == [(21.0, 26.0, 36.0)]
    assert mqtt_client.publish_telemetry.call_args.args[0]["waerme_fehlt"] is True


# --- Plan 3b: Energie-Normalisierung (Tageszaehler) ---

ENERGY_MANIFEST = ChannelManifest(refs={"energy_thermal_heating": "sensor.waerme_heute"})


def test_daily_energy_is_sent_as_a_growing_sum_and_survives_a_restart(make_store):
    ha_api = MagicMock()
    store = make_store()
    for raw, expected in ((4.0, 4.0), (6.0, 6.0), (0.5, 6.5)):
        ha_api.get_state.return_value = raw
        fields = telemetry.read_kpi_fields(ENERGY_MANIFEST, ha_api, telemetry.energy_normalizer(store, "daily"))
        assert fields["energy"] == {"thermal_heating": expected}
    restarted = make_store()  # backup.json neu eingelesen
    ha_api.get_state.return_value = 1.5
    fields = telemetry.read_kpi_fields(ENERGY_MANIFEST, ha_api, telemetry.energy_normalizer(restarted, "daily"))
    assert fields["energy"] == {"thermal_heating": 7.5}


def test_unreadable_daily_energy_is_left_out_and_keeps_the_state(make_store):
    store = make_store(backup={"energy_state": {"thermal_heating": {"raw": 4.0, "sum": 10.0}}})
    for reader in (MagicMock(side_effect=ValueError("unavailable")), MagicMock(return_value=float("inf"))):
        ha_api = MagicMock(get_state=reader)
        fields = telemetry.read_kpi_fields(ENERGY_MANIFEST, ha_api, telemetry.energy_normalizer(store, "daily"))
        assert "energy" not in fields
    assert store.state.energy_state == {"thermal_heating": {"raw": 4.0, "sum": 10.0}}


def test_total_counters_get_no_normalizer():
    assert telemetry.energy_normalizer(object(), "total") is None
