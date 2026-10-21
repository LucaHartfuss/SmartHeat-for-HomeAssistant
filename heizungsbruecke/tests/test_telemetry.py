import importlib.util
import logging
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fakes import FREMDTEXT, FREMDTEXT_IM_LOG, logzeilen

from smartheat_runtime import telemetry
from smartheat_runtime.delivery import DataFault
from smartheat_runtime.ports import MqttChannel, SignalSource, SignalUngueltig, SourceUnavailable
from smartheat_runtime.roles import ChannelManifest


def test_publish_telemetry_publishes_payload():
    mqtt_client = MagicMock(spec=MqttChannel)

    telemetry.publish_telemetry(mqtt_client=mqtt_client, room_actual=20.5, boost_active=False, failsafe_active=False)

    mqtt_client.publish_telemetry.assert_called_once()
    payload = mqtt_client.publish_telemetry.call_args.args[0]
    assert payload["room_actual"] == 20.5
    assert payload["boost_active"] is False
    assert payload["failsafe_active"] is False
    assert "ts" in payload


def test_run_telemetry_tick_reads_room_actual_and_publishes():
    manifest = ChannelManifest(refs={"room_actual": "sensor.room_actual"})
    ha_api = MagicMock(spec=SignalSource)
    ha_api.get_state.return_value = 20.5
    mqtt_client = MagicMock(spec=MqttChannel)

    telemetry.run_telemetry_tick(
        manifest, ha_api, mqtt_client, boost_active=True, failsafe_active=False,
    )

    ha_api.get_state.assert_called_once_with("sensor.room_actual")
    mqtt_client.publish_telemetry.assert_called_once()
    payload = mqtt_client.publish_telemetry.call_args.args[0]
    assert payload["room_actual"] == 20.5
    assert payload["boost_active"] is True
    assert payload["failsafe_active"] is False


@pytest.mark.parametrize("wert", [float("nan"), float("inf"), float("-inf")])
def test_run_telemetry_tick_sendet_kein_nicht_endliches_room_actual(wert, caplog):
    """Testoffensive T2, Nr. 17: room_actual ist Pflicht der Telemetrie (der Server lehnt sie sonst ab); ein
    nicht endlicher Wert wird nicht gesendet (Warnung), der naechste Tick liest erneut."""
    manifest = ChannelManifest(refs={"room_actual": "sensor.room_actual"})
    ha_api = MagicMock(spec=SignalSource)
    ha_api.get_state.return_value = wert
    mqtt_client = MagicMock(spec=MqttChannel)

    telemetry.run_telemetry_tick(manifest, ha_api, mqtt_client, boost_active=False, failsafe_active=False)

    mqtt_client.publish_telemetry.assert_not_called()
    assert "room_actual" in caplog.text


def test_ein_ausfall_von_room_actual_warnt_einmal_und_die_wiederaufnahme_meldet_eine_info_zeile(caplog):
    """Je Takt eine Telemetrie; faellt room_actual aus (NaN), entfaellt sie: eine WARNING beim Ausfall, weitere
    Ausfall-Takte nur DEBUG, bei der Wiederaufnahme eine INFO-Zeile (erst nach gelungenem Senden) und wieder eine
    Telemetrie je Takt."""
    manifest = ChannelManifest(refs={"room_actual": "sensor.room_actual"})
    ha_api = MagicMock(spec=SignalSource)
    mqtt_client = MagicMock(spec=MqttChannel)

    def takt(wert):
        ha_api.get_state.return_value = wert
        telemetry.run_telemetry_tick(manifest, ha_api, mqtt_client, boost_active=False, failsafe_active=False)
        return mqtt_client.publish_telemetry.call_count

    def zeilen(stufe):
        return [r.getMessage() for r in caplog.records if r.levelno == stufe and "room_actual" in r.getMessage()]

    with caplog.at_level(logging.DEBUG, logger="smartheat_runtime.telemetry"):
        assert [takt(20.5), takt(20.6)] == [1, 2]
        assert zeilen(logging.WARNING) == [] and zeilen(logging.INFO) == []
        assert [takt(float("nan")), takt(float("nan")), takt(float("inf"))] == [2, 2, 2]
        assert len(zeilen(logging.WARNING)) == 1 and len(zeilen(logging.DEBUG)) == 2
        assert zeilen(logging.INFO) == []
        mqtt_client.publish_telemetry.side_effect = OSError("Verbindung weg")
        assert takt(20.7) == 3
        assert zeilen(logging.INFO) == []  # Senden gescheitert: noch keine Wiederaufnahme
        mqtt_client.publish_telemetry.side_effect = None
        assert [takt(20.7), takt(20.8)] == [4, 5]
        assert len(zeilen(logging.INFO)) == 1 and len(zeilen(logging.WARNING)) == 1
        assert takt(float("nan")) == 5  # ein neuer Ausfall warnt wieder
        assert len(zeilen(logging.WARNING)) == 2
    assert mqtt_client.publish_telemetry.call_args.args[0]["room_actual"] == 20.8


def test_ein_ungueltiges_signal_warnt_einmal_ohne_traceback_bis_zum_naechsten_gelungenen_senden(caplog):
    """L4-17: SignalUngueltig (Fuehler liefert keinen gueltigen Wert) ist erwartet: eine WARNING ohne Traceback, weitere
    Takte nur DEBUG, nach gelungenem Senden wieder eine WARNING; kein ERROR."""
    manifest = ChannelManifest(refs={"room_actual": "sensor.room_actual"})
    ha_api = MagicMock(spec=SignalSource)
    mqtt_client = MagicMock(spec=MqttChannel)

    def takt(wert):
        ha_api.get_state.side_effect = wert if isinstance(wert, Exception) else None
        ha_api.get_state.return_value = wert
        telemetry.run_telemetry_tick(manifest, ha_api, mqtt_client, boost_active=False, failsafe_active=False)

    def zeilen(stufe):
        return [r for r in caplog.records if r.levelno == stufe and r.name == "smartheat_runtime.telemetry"]

    ungueltig = SignalUngueltig("could not convert string to float: 'unavailable'")
    with caplog.at_level(logging.DEBUG, logger="smartheat_runtime.telemetry"):
        for _ in range(3):
            takt(ungueltig)
        assert len(zeilen(logging.WARNING)) == 1 and len(zeilen(logging.DEBUG)) == 2
        assert zeilen(logging.ERROR) == [] and zeilen(logging.WARNING)[0].exc_info is None
        assert "could not convert string to float: 'unavailable'" in zeilen(logging.WARNING)[0].getMessage()
        takt(20.5)
        mqtt_client.publish_telemetry.assert_called_once()
        takt(ungueltig)  # nach einem gelungenen Senden warnt ein neuer Ausfall wieder
        assert len(zeilen(logging.WARNING)) == 2 and zeilen(logging.ERROR) == []


@pytest.mark.parametrize("fehler", [ValueError("x"), RuntimeError("Hickup")])
def test_ein_anderer_fehler_der_telemetrie_bleibt_ein_error_mit_traceback(fehler, caplog):
    manifest = ChannelManifest(refs={"room_actual": "sensor.room_actual"})
    ha_api = MagicMock(spec=SignalSource)
    ha_api.get_state.side_effect = fehler
    mqtt_client = MagicMock(spec=MqttChannel)

    with caplog.at_level(logging.DEBUG, logger="smartheat_runtime.telemetry"):
        telemetry.run_telemetry_tick(manifest, ha_api, mqtt_client, boost_active=False, failsafe_active=False)

    (zeile,) = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert zeile.exc_info is not None and "Fehler beim Veroeffentlichen" in zeile.getMessage()


def test_run_telemetry_tick_includes_configured_optional_kpi_fields():
    manifest = ChannelManifest(refs={
        "room_actual": "sensor.room_actual",
        "flow_temperature": "sensor.flow",
        "operating_mode": "sensor.mode",
    })
    ha_api = MagicMock(spec=SignalSource)
    ha_api.get_state.side_effect = lambda entity_id: {
        "sensor.room_actual": 20.5, "sensor.flow": 45.2,
    }[entity_id]
    ha_api.get_raw_state.return_value = "heating"
    mqtt_client = MagicMock(spec=MqttChannel)

    telemetry.run_telemetry_tick(
        manifest, ha_api, mqtt_client, boost_active=False, failsafe_active=False,
    )

    payload = mqtt_client.publish_telemetry.call_args.args[0]
    assert payload["flow_temperature"] == 45.2
    assert payload["operating_mode"] == "heating"
    ha_api.get_raw_state.assert_called_once_with("sensor.mode")


def test_run_telemetry_tick_omits_unconfigured_optional_kpi_fields():
    manifest = ChannelManifest(refs={"room_actual": "sensor.room_actual"})
    ha_api = MagicMock(spec=SignalSource)
    ha_api.get_state.return_value = 20.5
    mqtt_client = MagicMock(spec=MqttChannel)

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
    ha_api = MagicMock(spec=SignalSource)
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
    ha_api = MagicMock(spec=SignalSource)
    ha_api.get_state.side_effect = lambda entity_id: {
        "sensor.room_actual": 20.5, "sensor.e_thermal": 1234.5, "sensor.e_elec": 300.1,
    }[entity_id]
    ha_api.get_unit.return_value = "kWh"
    mqtt_client = MagicMock(spec=MqttChannel)

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
    ha_api = MagicMock(spec=SignalSource)

    def fake_get_state(entity_id):
        if entity_id == "sensor.flow":
            raise ValueError("could not convert string to float: 'unavailable'")
        return {"sensor.room_actual": 20.5, "sensor.ret": 30.0}[entity_id]

    ha_api.get_state.side_effect = fake_get_state
    ha_api.get_raw_state.return_value = "heating"
    mqtt_client = MagicMock(spec=MqttChannel)

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
    ha_api = MagicMock(spec=SignalSource)
    ha_api.get_state.return_value = 20.5
    ha_api.get_raw_state.side_effect = ValueError("unavailable")
    mqtt_client = MagicMock(spec=MqttChannel)

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
    ha_api = MagicMock(spec=SignalSource)
    ha_api.get_state.side_effect = lambda entity_id: {
        "sensor.room_actual": 20.5, "sensor.flow": float("nan"), "sensor.ret": 30.0,
        "sensor.e1": float("inf"), "sensor.e2": 12.0,
    }[entity_id]
    ha_api.get_unit.return_value = "kWh"
    mqtt_client = MagicMock(spec=MqttChannel)

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
    ha_api = MagicMock(spec=SignalSource)

    def fake_get_state(entity_id):
        if entity_id == "sensor.room_actual":
            return 20.5
        raise ValueError("unavailable")

    ha_api.get_state.side_effect = fake_get_state
    ha_api.get_unit.return_value = "kWh"
    mqtt_client = MagicMock(spec=MqttChannel)

    telemetry.run_telemetry_tick(
        manifest, ha_api, mqtt_client, boost_active=False, failsafe_active=False,
        einheiten={"thermal_heating": "kWh", "thermal_dhw": "kWh"},
    )

    payload = mqtt_client.publish_telemetry.call_args.args[0]
    assert payload["room_actual"] == 20.5
    assert "energy" not in payload


def test_run_telemetry_tick_skips_when_room_actual_not_mapped():
    manifest = ChannelManifest(refs={})
    ha_api = MagicMock(spec=SignalSource)
    mqtt_client = MagicMock(spec=MqttChannel)

    telemetry.run_telemetry_tick(
        manifest, ha_api, mqtt_client, boost_active=False, failsafe_active=False,
    )

    ha_api.get_state.assert_not_called()
    mqtt_client.publish_telemetry.assert_not_called()


def test_publish_telemetry_includes_datenfehler_when_given():
    mqtt_client = MagicMock(spec=MqttChannel)

    telemetry.publish_telemetry(
        mqtt_client=mqtt_client, room_actual=20.5, boost_active=False, failsafe_active=False,
        datenfehler=DataFault("write", ("curve", "number.x", "Timeout")),
    )

    payload = mqtt_client.publish_telemetry.call_args.args[0]
    assert payload["datenfehler"] == {"source": "write", "detail": ["curve", "number.x", "Timeout"]}


def test_publish_telemetry_omits_datenfehler_without_fault():
    mqtt_client = MagicMock(spec=MqttChannel)

    telemetry.publish_telemetry(mqtt_client=mqtt_client, room_actual=20.5, boost_active=False, failsafe_active=False)

    assert telemetry.DATENFEHLER_KEY not in mqtt_client.publish_telemetry.call_args.args[0]


def test_run_telemetry_tick_passes_datenfehler_through():
    manifest = ChannelManifest(refs={"room_actual": "sensor.room_actual"})
    ha_api = MagicMock(spec=SignalSource)
    ha_api.get_state.return_value = 20.5
    mqtt_client = MagicMock(spec=MqttChannel)

    telemetry.run_telemetry_tick(
        manifest, ha_api, mqtt_client, boost_active=False, failsafe_active=False,
        datenfehler=DataFault("local", ("dat",)),
    )

    assert mqtt_client.publish_telemetry.call_args.args[0]["datenfehler"] == {"source": "local", "detail": ["dat"]}


def test_run_telemetry_tick_survives_exception_without_propagating(monkeypatch, caplog):
    manifest = ChannelManifest(refs={"room_actual": "sensor.room_actual"})
    ha_api = MagicMock(spec=SignalSource)
    ha_api.get_state.side_effect = OSError("Datentraeger voll")
    mqtt_client = MagicMock(spec=MqttChannel)

    with caplog.at_level("ERROR"):
        telemetry.run_telemetry_tick(
            manifest, ha_api, mqtt_client, boost_active=False, failsafe_active=False,
        )  # must not raise

    assert "Telemetrie" in caplog.text


def test_regulation_fields_and_local_ts():
    manifest = SimpleNamespace(refs={
        "room_actual": "sensor.r", "outdoor_temp": "sensor.o", "flow_setpoint": "sensor.vl",
    })
    ha = MagicMock(spec=SignalSource)
    ha.get_state.side_effect = {"sensor.r": 20.1, "sensor.o": 3.5, "sensor.vl": 41.0}.__getitem__
    mqtt = MagicMock(spec=MqttChannel)
    telemetry.run_telemetry_tick(manifest, ha, mqtt, boost_active=False, failsafe_active=False, room_target=20.5)
    payload = mqtt.publish_telemetry.call_args.args[0]
    assert (payload["room_target"], payload["outdoor_temp"], payload["flow_setpoint"]) == (20.5, 3.5, 41.0)
    assert datetime.fromisoformat(payload["ts"]).tzinfo is not None


def test_read_regulation_fields_liest_aussentemperatur_und_vorlaufsoll_aus_den_signalen():
    manifest = SimpleNamespace(refs={"outdoor_temp": "sensor.o", "flow_setpoint": "sensor.vl", "room_target": "x.y"})
    signals = MagicMock(spec=SignalSource)
    signals.get_state.side_effect = {"sensor.o": 3.5, "sensor.vl": 41.0, "x.y": 99.0}.__getitem__
    assert telemetry.read_regulation_fields(manifest, signals, 20.5) == {
        "room_target": 20.5, "outdoor_temp": 3.5, "flow_setpoint": 41.0}  # room_target aus dem Argument, nicht 99.0
    assert sorted(call.args[0] for call in signals.get_state.call_args_list) == ["sensor.o", "sensor.vl"]  # nie "x.y"


def test_unreadable_regulation_field_is_omitted():
    manifest = SimpleNamespace(refs={"room_actual": "sensor.r", "outdoor_temp": "sensor.o"})
    ha = MagicMock(spec=SignalSource)

    def _get(ref):
        if ref == "sensor.o":
            raise RuntimeError("unavailable")
        return 20.1
    ha.get_state.side_effect = _get
    mqtt = MagicMock(spec=MqttChannel)
    telemetry.run_telemetry_tick(manifest, ha, mqtt, boost_active=False, failsafe_active=False, room_target=None)
    payload = mqtt.publish_telemetry.call_args.args[0]
    assert "outdoor_temp" not in payload and "room_target" not in payload


def test_delivery_roles_are_read_and_published():
    manifest = ChannelManifest(refs={
        "room_actual": "sensor.room", "generator_hours": "sensor.h", "generator_starts": "sensor.s",
        "generator_state": "sensor.b",
    })
    ha_api = MagicMock(spec=SignalSource)
    ha_api.get_state.side_effect = lambda entity_id: {"sensor.room": 20.5, "sensor.h": 1234.5, "sensor.s": 678.0}[entity_id]
    ha_api.get_raw_state.side_effect = lambda entity_id: {"sensor.b": "HEATING"}[entity_id]
    mqtt_client = MagicMock(spec=MqttChannel)

    telemetry.run_telemetry_tick(manifest, ha_api, mqtt_client, boost_active=False, failsafe_active=False)

    payload = mqtt_client.publish_telemetry.call_args.args[0]
    assert (payload["generator_hours"], payload["generator_starts"], payload["generator_state"]) == (
        1234.5, 678.0, "HEATING",
    )


def test_unreadable_delivery_roles_are_left_out_and_unmapped_ones_are_not_read():
    manifest = ChannelManifest(refs={"room_actual": "sensor.room", "generator_state": "sensor.b"})
    ha_api = MagicMock(spec=SignalSource)
    ha_api.get_state.return_value = 20.5
    ha_api.get_raw_state.side_effect = RuntimeError("unavailable")
    mqtt_client = MagicMock(spec=MqttChannel)

    telemetry.run_telemetry_tick(manifest, ha_api, mqtt_client, boost_active=False, failsafe_active=False)

    payload = mqtt_client.publish_telemetry.call_args.args[0]
    assert not any(role in payload for role in telemetry.DELIVERY_ROLES)


def _telemetrie_aus_quelle(tmp_path, alt: str, neu: str):
    """Fuehrt die Quelle von telemetry.py als eigenes Modul aus, mit `alt` durch `neu` ersetzt."""
    quelle = Path(telemetry.__file__).read_text()
    assert alt in quelle
    kopie = tmp_path / "telemetrie_kopie.py"
    kopie.write_text(quelle.replace(alt, neu))
    spec = importlib.util.spec_from_file_location("telemetrie_kopie", kopie)
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


def test_ein_liefer_signal_ohne_kpi_leser_haelt_den_import_an(tmp_path):
    alt = 'DELIVERY_ROLES = ("generator_hours", "generator_starts", "generator_state")'
    assert _telemetrie_aus_quelle(tmp_path, alt, alt).DELIVERY_ROLES == (  # Gegenprobe: unveraendert laedt sie
        "generator_hours", "generator_starts", "generator_state")
    with pytest.raises(RuntimeError, match="DELIVERY_ROLES nennt eine Rolle ausserhalb der KPI-Rollen"):
        _telemetrie_aus_quelle(tmp_path, alt, alt.replace('"generator_state"', '"generator_state", "brenner_takte"'))


def test_kpi_role_constants():
    assert telemetry.KPI_TEXT_ROLES == ("operating_mode", "generator_state")
    assert "generator_hours" in telemetry.KPI_NUMERIC_ROLES and "generator_starts" in telemetry.KPI_NUMERIC_ROLES


def test_the_telemetry_no_longer_carries_waerme_fehlt():
    mqtt_client = MagicMock(spec=MqttChannel)
    telemetry.publish_telemetry(mqtt_client=mqtt_client, room_actual=20.5, boost_active=False, failsafe_active=False)
    assert "waerme_fehlt" not in mqtt_client.publish_telemetry.call_args.args[0]


# --- Plan 3b: Energie-Normalisierung (Tageszaehler) ---

ENERGY_MANIFEST = ChannelManifest(refs={"energy_thermal_heating": "sensor.waerme_heute"})
KWH = {"thermal_heating": "kWh"}


def test_daily_energy_is_sent_as_a_growing_sum_and_survives_a_restart(make_store):
    ha_api = MagicMock(spec=SignalSource)
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
    for lesen in ({"side_effect": ValueError("unavailable")}, {"return_value": float("inf")}):
        ha_api = MagicMock(spec=SignalSource)
        ha_api.get_state.configure_mock(**lesen)
        ha_api.get_unit.return_value = "kWh"
        fields = telemetry.read_kpi_fields(
            ENERGY_MANIFEST, ha_api, telemetry.energy_normalizer(store, "daily"), einheiten=KWH)
        assert "energy" not in fields
    assert store.state.energy_state == {"thermal_heating": {"raw": 4.0, "sum": 10.0, "einheit": "kWh"}}


def test_der_zaehlerzustand_aus_1_0_0_beginnt_neu(make_store):
    """Zustand ohne Einheit (1.0.0, Wh) ist lesbar und beginnt in kWh neu, statt die Wh-Summe fortzusetzen."""
    store = make_store(backup={"energy_state": {"thermal_heating": {"raw": 4000.0, "sum": 900000.0}}})
    ha_api = MagicMock(spec=SignalSource)
    ha_api.get_state.return_value = 4.5
    ha_api.get_unit.return_value = "kWh"
    fields = telemetry.read_kpi_fields(
        ENERGY_MANIFEST, ha_api, telemetry.energy_normalizer(store, "daily"), einheiten=KWH)
    assert fields["energy"] == {"thermal_heating": 4.5}
    assert store.state.energy_state == {"thermal_heating": {"raw": 4.5, "sum": 4.5, "einheit": "kWh"}}


def test_daily_energy_in_wh_wird_als_kwh_summiert(make_store):
    store = make_store()
    ha_api = MagicMock(spec=SignalSource)
    ha_api.get_unit.return_value = "Wh"
    for raw, expected in ((4000.0, 4.0), (6500.0, 6.5)):
        ha_api.get_state.return_value = raw
        fields = telemetry.read_kpi_fields(
            ENERGY_MANIFEST, ha_api, telemetry.energy_normalizer(store, "daily"), einheiten={"thermal_heating": "Wh"})
        assert fields["energy"] == {"thermal_heating": expected}
    assert store.state.energy_state["thermal_heating"]["einheit"] == "kWh"


def test_total_counters_get_no_normalizer():
    assert telemetry.energy_normalizer(object(), "total") is None


# --- Spec Stufe 3 S4b 5 (A5-11): Energie nur mit der erwarteten Einheit ---

def test_energie_in_wh_wird_als_kwh_gesendet():
    manifest = ChannelManifest(refs={"energy_primary_heating": "sensor.gas"})
    ha_api = MagicMock(spec=SignalSource)
    ha_api.get_state.return_value = 2500.0
    ha_api.get_unit.return_value = "Wh"
    fields = telemetry.read_kpi_fields(manifest, ha_api, einheiten={"primary_heating": "Wh"})
    assert fields["energy"] == {"primary_heating": 2.5}


def test_fremde_einheit_laesst_den_kanal_weg(caplog):
    manifest = ChannelManifest(refs={"energy_primary_heating": "sensor.gas", "energy_primary_dhw": "sensor.ww"})
    ha_api = MagicMock(spec=SignalSource)
    ha_api.get_state.return_value = 2.0
    ha_api.get_unit.side_effect = lambda ref: {"sensor.gas": "kWh", "sensor.ww": "Wh"}[ref]
    fields = telemetry.read_kpi_fields(manifest, ha_api, einheiten={"primary_heating": "Wh", "primary_dhw": "Wh"})
    assert fields["energy"] == {"primary_dhw": 0.002}
    assert any("primary_heating" in r.getMessage() for r in caplog.records)


def test_kanal_ohne_erwartete_einheit_wird_nicht_gelesen():
    manifest = ChannelManifest(refs={"energy_primary_heating": "sensor.gas"})
    ha_api = MagicMock(spec=SignalSource)
    fields = telemetry.read_kpi_fields(manifest, ha_api, einheiten={})
    assert "energy" not in fields
    ha_api.get_state.assert_not_called()


def test_fehlende_einheit_warnt_einmal_je_wechsel(caplog):
    manifest = ChannelManifest(refs={"energy_primary_heating": "sensor.gas"})
    ha_api = MagicMock(spec=SignalSource)
    ha_api.get_state.return_value = 2.0
    einheiten = {"primary_heating": "kWh"}

    def warnungen():
        return [r for r in caplog.records if r.levelno == logging.WARNING]

    ha_api.get_unit.side_effect = ValueError("kein Attribut")
    for _ in range(3):
        assert "energy" not in telemetry.read_kpi_fields(manifest, ha_api, einheiten=einheiten)
    assert len(warnungen()) == 1 and "keine Einheit statt kWh" in warnungen()[0].getMessage()
    ha_api.get_unit.side_effect = None
    ha_api.get_unit.return_value = "kWh"  # Attribut da: Kanal kommt zurueck
    assert telemetry.read_kpi_fields(manifest, ha_api, einheiten=einheiten)["energy"] == {"primary_heating": 2.0}
    ha_api.get_unit.side_effect = ValueError("kein Attribut")  # wieder weg: erneute Warnung
    assert "energy" not in telemetry.read_kpi_fields(manifest, ha_api, einheiten=einheiten)
    assert len(warnungen()) == 2


def test_voruebergehender_fehler_beim_lesen_der_einheit_laesst_den_kanal_weg_ohne_wechselwarnung(caplog):
    manifest = ChannelManifest(refs={"energy_primary_heating": "sensor.gas"})
    ha_api = MagicMock(spec=SignalSource)
    ha_api.get_state.return_value = 2.0
    ha_api.get_unit.side_effect = SourceUnavailable("HA weg")
    assert "energy" not in telemetry.read_kpi_fields(manifest, ha_api, einheiten={"primary_heating": "kWh"})
    assert telemetry._EINHEIT_GEWARNT == {}
    assert any("nicht lesbar" in r.getMessage() for r in caplog.records)


def test_die_warnung_zur_fremden_einheit_kommt_einmal_je_wechsel(caplog):
    manifest = ChannelManifest(refs={"energy_primary_heating": "sensor.gas"})
    ha_api = MagicMock(spec=SignalSource)
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


def _regelfeld(ha_api):
    ha_api.get_state.side_effect = RuntimeError(FREMDTEXT)
    telemetry.read_regulation_fields(ChannelManifest(refs={"outdoor_temp": "sensor.aussen"}), ha_api, None)
    return f"Regel-Telemetrie 'outdoor_temp' nicht lesbar, Feld wird weggelassen: {FREMDTEXT_IM_LOG}"


def _kpi_wert(ha_api):
    ha_api.get_state.side_effect = RuntimeError(FREMDTEXT)
    telemetry.read_kpi_fields(ChannelManifest(refs={"flow_temperature": "sensor.vorlauf"}), ha_api)
    return f"KPI-Sensor 'flow_temperature' nicht lesbar, Feld wird weggelassen: {FREMDTEXT_IM_LOG}"


def _einheit_nicht_lesbar(ha_api):
    ha_api.get_state.return_value = 2.0
    ha_api.get_unit.side_effect = SourceUnavailable(FREMDTEXT)
    telemetry.read_kpi_fields(ChannelManifest(refs={"energy_primary_heating": "sensor.gas"}), ha_api,
                              einheiten={"primary_heating": "kWh"})
    return f"KPI-Sensor 'energy_primary_heating' nicht lesbar, Feld wird weggelassen: {FREMDTEXT_IM_LOG}"


def _fremde_einheit(ha_api):
    ha_api.get_state.return_value = 2.0
    ha_api.get_unit.return_value = FREMDTEXT
    telemetry.read_kpi_fields(ChannelManifest(refs={"energy_primary_heating": "sensor.gas"}), ha_api,
                              einheiten={"primary_heating": "kWh"})
    return f"Energiekanal 'primary_heating' meldet '{FREMDTEXT_IM_LOG}' statt kWh und wird weggelassen"


@pytest.mark.parametrize("pfad", [_regelfeld, _kpi_wert, _einheit_nicht_lesbar, _fremde_einheit],
                         ids=["regelfeld", "kpi_wert", "einheit_nicht_lesbar", "fremde_einheit"])
def test_fremder_text_aus_der_signalquelle_steht_nur_escaped_im_log(caplog, pfad):
    """T6-Rest, R-c: der Fehlertext beim Lesen (Regelfeld, KPI-Wert, Einheit) und die gemeldete Einheit selbst kommen
    aus HA bzw. vom lokalen Bus; in der Logzeile stehen sie nur als Escape (Zeilenumbruch, U+202E, U+061C)."""
    ha_api = MagicMock(spec=SignalSource)
    with caplog.at_level(logging.WARNING, logger="smartheat_runtime.telemetry"):
        zeile = pfad(ha_api)
    assert zeile in logzeilen(caplog)
