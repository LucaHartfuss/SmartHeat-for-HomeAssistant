from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

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
    ha_api.get_unit.return_value = "kWh"

    fields = telemetry.read_kpi_fields(manifest, ha_api, einheiten={"electrical_total": "kWh"})

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
    ha_api.get_unit.return_value = "kWh"
    mqtt_client = MagicMock()

    telemetry.run_telemetry_tick(
        manifest, ha_api, mqtt_client, boost_active=False, failsafe_active=False,
        einheiten={"thermal_heating": "kWh", "electrical_heating": "kWh"},
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
    ha_api.get_unit.return_value = "kWh"
    mqtt_client = MagicMock()

    telemetry.run_telemetry_tick(
        manifest, ha_api, mqtt_client, boost_active=False, failsafe_active=False,
        einheiten={"thermal_heating": "kWh", "thermal_dhw": "kWh"},
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
    ha_api.get_unit.return_value = "kWh"
    mqtt_client = MagicMock()

    telemetry.run_telemetry_tick(
        manifest, ha_api, mqtt_client, boost_active=False, failsafe_active=False,
        einheiten={"thermal_heating": "kWh", "thermal_dhw": "kWh"},
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


def test_delivery_roles_are_read_and_published():
    manifest = ChannelManifest(refs={
        "room_actual": "sensor.room", "generator_hours": "sensor.h", "generator_starts": "sensor.s",
        "generator_state": "sensor.b",
    })
    ha_api = MagicMock()
    ha_api.get_state.side_effect = lambda entity_id: {"sensor.room": 20.5, "sensor.h": 1234.5, "sensor.s": 678.0}[entity_id]
    ha_api.get_raw_state.side_effect = lambda entity_id: {"sensor.b": "HEATING"}[entity_id]
    mqtt_client = MagicMock()

    telemetry.run_telemetry_tick(manifest, ha_api, mqtt_client, boost_active=False, failsafe_active=False)

    payload = mqtt_client.publish_telemetry.call_args.args[0]
    assert (payload["generator_hours"], payload["generator_starts"], payload["generator_state"]) == (
        1234.5, 678.0, "HEATING",
    )


def test_unreadable_delivery_roles_are_left_out_and_unmapped_ones_are_not_read():
    manifest = ChannelManifest(refs={"room_actual": "sensor.room", "generator_state": "sensor.b"})
    ha_api = MagicMock()
    ha_api.get_state.return_value = 20.5
    ha_api.get_raw_state.side_effect = RuntimeError("unavailable")
    mqtt_client = MagicMock()

    telemetry.run_telemetry_tick(manifest, ha_api, mqtt_client, boost_active=False, failsafe_active=False)

    payload = mqtt_client.publish_telemetry.call_args.args[0]
    assert not any(role in payload for role in telemetry.DELIVERY_ROLES)


def test_kpi_role_constants():
    assert telemetry.KPI_TEXT_ROLES == ("operating_mode", "generator_state")
    assert "generator_hours" in telemetry.KPI_NUMERIC_ROLES and "generator_starts" in telemetry.KPI_NUMERIC_ROLES


def test_the_telemetry_no_longer_carries_waerme_fehlt():
    mqtt_client = MagicMock()
    telemetry.publish_telemetry(mqtt_client=mqtt_client, room_actual=20.5, boost_active=False, failsafe_active=False)
    assert "waerme_fehlt" not in mqtt_client.publish_telemetry.call_args.args[0]


# --- Plan 3b: Energie-Normalisierung (Tageszaehler) ---

ENERGY_MANIFEST = ChannelManifest(refs={"energy_thermal_heating": "sensor.waerme_heute"})
KWH = {"thermal_heating": "kWh"}


def test_daily_energy_is_sent_as_a_growing_sum_and_survives_a_restart(make_store):
    ha_api = MagicMock()
    ha_api.get_unit.return_value = "kWh"
    store = make_store()
    for raw, expected in ((4.0, 4.0), (6.0, 6.0), (0.5, 6.5)):
        ha_api.get_state.return_value = raw
        fields = telemetry.read_kpi_fields(
            ENERGY_MANIFEST, ha_api, telemetry.energy_normalizer(store, "daily"), einheiten=KWH)
        assert fields["energy"] == {"thermal_heating": expected}
    restarted = make_store()  # backup.json neu eingelesen
    ha_api.get_state.return_value = 1.5
    fields = telemetry.read_kpi_fields(
        ENERGY_MANIFEST, ha_api, telemetry.energy_normalizer(restarted, "daily"), einheiten=KWH)
    assert fields["energy"] == {"thermal_heating": 7.5}


def test_unreadable_daily_energy_is_left_out_and_keeps_the_state(make_store):
    store = make_store(backup={"energy_state": {"thermal_heating": {"raw": 4.0, "sum": 10.0, "einheit": "kWh"}}})
    for reader in (MagicMock(side_effect=ValueError("unavailable")), MagicMock(return_value=float("inf"))):
        ha_api = MagicMock(get_state=reader, get_unit=MagicMock(return_value="kWh"))
        fields = telemetry.read_kpi_fields(
            ENERGY_MANIFEST, ha_api, telemetry.energy_normalizer(store, "daily"), einheiten=KWH)
        assert "energy" not in fields
    assert store.state.energy_state == {"thermal_heating": {"raw": 4.0, "sum": 10.0, "einheit": "kWh"}}


def test_der_zaehlerzustand_aus_1_0_0_beginnt_neu(make_store):
    """Zustand ohne Einheit (1.0.0, Wh) ist lesbar und beginnt in kWh neu, statt die Wh-Summe fortzusetzen."""
    store = make_store(backup={"energy_state": {"thermal_heating": {"raw": 4000.0, "sum": 900000.0}}})
    ha_api = MagicMock(get_state=MagicMock(return_value=4.5), get_unit=MagicMock(return_value="kWh"))
    fields = telemetry.read_kpi_fields(
        ENERGY_MANIFEST, ha_api, telemetry.energy_normalizer(store, "daily"), einheiten=KWH)
    assert fields["energy"] == {"thermal_heating": 4.5}
    assert store.state.energy_state == {"thermal_heating": {"raw": 4.5, "sum": 4.5, "einheit": "kWh"}}


def test_daily_energy_in_wh_wird_als_kwh_summiert(make_store):
    store = make_store()
    ha_api = MagicMock(get_unit=MagicMock(return_value="Wh"))
    for raw, expected in ((4000.0, 4.0), (6500.0, 6.5)):
        ha_api.get_state.return_value = raw
        fields = telemetry.read_kpi_fields(
            ENERGY_MANIFEST, ha_api, telemetry.energy_normalizer(store, "daily"), einheiten={"thermal_heating": "Wh"})
        assert fields["energy"] == {"thermal_heating": expected}
    assert store.state.energy_state["thermal_heating"]["einheit"] == "kWh"


def test_total_counters_get_no_normalizer():
    assert telemetry.energy_normalizer(object(), "total") is None


# --- Spec Stufe 3 S4b 5 (A5-11): Energie nur mit der erwarteten Einheit ---

@pytest.fixture(autouse=True)
def _einheit_warnung_zuruecksetzen():
    telemetry._EINHEIT_GEWARNT.clear()
    yield
    telemetry._EINHEIT_GEWARNT.clear()


def test_energie_in_wh_wird_als_kwh_gesendet():
    manifest = ChannelManifest(refs={"energy_primary_heating": "sensor.gas"})
    ha_api = MagicMock()
    ha_api.get_state.return_value = 2500.0
    ha_api.get_unit.return_value = "Wh"
    fields = telemetry.read_kpi_fields(manifest, ha_api, einheiten={"primary_heating": "Wh"})
    assert fields["energy"] == {"primary_heating": 2.5}


def test_fremde_einheit_laesst_den_kanal_weg(caplog):
    manifest = ChannelManifest(refs={"energy_primary_heating": "sensor.gas", "energy_primary_dhw": "sensor.ww"})
    ha_api = MagicMock()
    ha_api.get_state.return_value = 2.0
    ha_api.get_unit.side_effect = lambda ref: {"sensor.gas": "kWh", "sensor.ww": "Wh"}[ref]
    fields = telemetry.read_kpi_fields(manifest, ha_api, einheiten={"primary_heating": "Wh", "primary_dhw": "Wh"})
    assert fields["energy"] == {"primary_dhw": 0.002}
    assert any("primary_heating" in r.getMessage() for r in caplog.records)


def test_kanal_ohne_erwartete_einheit_wird_nicht_gelesen():
    manifest = ChannelManifest(refs={"energy_primary_heating": "sensor.gas"})
    ha_api = MagicMock()
    fields = telemetry.read_kpi_fields(manifest, ha_api, einheiten={})
    assert "energy" not in fields
    ha_api.get_state.assert_not_called()


def test_nicht_lesbare_einheit_laesst_den_kanal_weg():
    manifest = ChannelManifest(refs={"energy_primary_heating": "sensor.gas"})
    ha_api = MagicMock()
    ha_api.get_state.return_value = 2.0
    ha_api.get_unit.side_effect = ValueError("kein Attribut")
    assert "energy" not in telemetry.read_kpi_fields(manifest, ha_api, einheiten={"primary_heating": "kWh"})


def test_die_warnung_zur_fremden_einheit_kommt_einmal_je_wechsel(caplog):
    manifest = ChannelManifest(refs={"energy_primary_heating": "sensor.gas"})
    ha_api = MagicMock()
    ha_api.get_state.return_value = 2.0
    einheiten = {"primary_heating": "kWh"}

    def warnungen():
        return [r for r in caplog.records if "meldet" in r.getMessage()]

    ha_api.get_unit.return_value = "MJ"
    for _ in range(3):
        telemetry.read_kpi_fields(manifest, ha_api, einheiten=einheiten)
    assert len(warnungen()) == 1
    ha_api.get_unit.return_value = "kWh"  # passt wieder: Kanal kommt zurueck
    assert telemetry.read_kpi_fields(manifest, ha_api, einheiten=einheiten)["energy"] == {"primary_heating": 2.0}
    ha_api.get_unit.return_value = "MJ"  # erneuter Wechsel: neue Warnung
    telemetry.read_kpi_fields(manifest, ha_api, einheiten=einheiten)
    assert len(warnungen()) == 2
