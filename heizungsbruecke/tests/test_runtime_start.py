"""Start der Laufzeit ohne eigene MQTT-Verbindung und ohne Abo-Abfrage (Phase 1 S1b, 5c-D-1): der Geraetekern haelt die
Verbindung (mqtt_factory ist Pflicht), den Abo-Status liefert das Konfigurationsdokument (abo_source ist Pflicht)."""
import dataclasses
import inspect

from fakes import runtime_config

from smartheat_runtime import app, entitlement
from smartheat_runtime.runtime_config import RuntimeConfig


def test_start_verlangt_mqtt_factory():
    parameter = inspect.signature(app.start).parameters["mqtt_factory"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


def test_abo_status_kommt_nur_aus_dem_dokument():
    config = runtime_config(abo_source=lambda: entitlement.INACTIVE)
    assert entitlement.query(config) == entitlement.INACTIVE


def test_runtime_config_hat_keine_altfelder():
    namen = {feld.name for feld in dataclasses.fields(RuntimeConfig)}
    assert namen == {
        "tenant_id", "setup_id", "lever_set_id", "local_safety", "daily_trigger_time", "local_check_interval",
        "telemetry_interval", "notify_hints_off", "room_sensor_refs", "battery_refs", "entitlement_path", "abo_source",
    }


def test_runtime_config_verlangt_abo_source():
    feld = next(f for f in dataclasses.fields(RuntimeConfig) if f.name == "abo_source")
    assert feld.default is dataclasses.MISSING and feld.default_factory is dataclasses.MISSING


def test_entitlement_kennt_keine_http_abfrage_mehr():
    assert {name for name in vars(entitlement) if name.startswith("query")} == {"query"}
    assert "requests" not in vars(entitlement)
    assert "STATUS_PATH" not in vars(entitlement)
