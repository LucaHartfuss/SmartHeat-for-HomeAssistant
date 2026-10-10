"""Spec 5b 5.2: Laufzeit aus dem Konfigurationsdokument (Plan D1 Task 9): BootInfo, RuntimeConfig ohne Transport,
Abo aus dem Dokument, Neustart nur bei laufzeitrelevanten Aenderungen."""
from pathlib import Path

import pytest

from smartheat_core.safety import resolve_local_safety
from smartheat_device import wire
from smartheat_runtime import dokument_config as dc
from smartheat_runtime import entitlement, mqtt_link, options
from smartheat_runtime.options import ConfigError
from smartheat_runtime.worker import RegulationWorker

SENSOR = "zigbee:0x00124b0000000001:temperature"
KONF = {
    "anlage": "kunde3", "profil": "viessmann_gastherme_heizkoerper", "hebelsatz": "viessmann_vicare",
    "verteilsystem": "Heizkoerper", "bindung": {"treiber": {"id": "simulation", "parameter": {}}, "thermostat": None},
    "raumfuehler": [SENSOR], "tagestick": "12:12", "abo": "aktiv", "mindest_software": "0.6.0", "software": None,
}


def _boot(konfiguration, tmp_path, abgemeldet=False):
    return dc.boot_info(konfiguration, abgemeldet=abgemeldet, client_version="0.6.0", backup_path=tmp_path / "b.json",
                        failsafe_path=tmp_path / "f.json")


def test_fields_are_contract_fields():
    assert set(dc.FELDER) <= set(wire.KONFIGURATION_FIELDS) | set(wire.KONFIGURATION_OPTIONAL)
    assert set(dc.LAUFZEIT_FELDER) <= set(dc.FELDER)


def test_boot_info_of_a_set_up_device(tmp_path):
    boot = _boot(KONF, tmp_path)
    assert (boot.configured, boot.signed_off, boot.tenant_id, boot.setup_id) == (True, False, "kunde3", "kunde3")
    assert (boot.lever_set.id, boot.client_version, boot.notify_hints_off) == ("viessmann_vicare", "0.6.0", ())
    assert boot.local_check_interval == options.DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS
    assert (boot.backup_path, boot.failsafe_path) == (tmp_path / "b.json", tmp_path / "f.json")


def test_boot_info_not_set_up_and_signed_off(tmp_path):
    assert (_boot(None, tmp_path).configured, _boot(None, tmp_path).signed_off) == (False, False)
    assert (_boot({"anlage": None}, tmp_path).configured, _boot({"anlage": None}, tmp_path).tenant_id) == (False, None)
    signed_off = _boot(KONF, tmp_path, abgemeldet=True)
    assert (signed_off.configured, signed_off.signed_off, signed_off.tenant_id) == (False, True, "kunde3")


def test_boot_info_never_throws(tmp_path):
    boot = _boot({"anlage": 5, "hebelsatz": "unbekannt", "intervalle": "x"}, tmp_path)
    assert boot.configured is False and boot.lever_set.id == options.DEFAULT_LEVER_SET


def test_runtime_config_from_the_document():
    def source():
        return entitlement.ACTIVE

    config = dc.runtime_config(KONF, entitlement_path=Path("/tmp/e.json"), abo_source=source)
    assert (config.descriptor, config.credential, config.installation_token, config.accounts_api_base_url) == (
        None, None, None, None)
    assert (config.tenant_id, config.setup_id, config.lever_set_id) == ("kunde3", "kunde3", "viessmann_vicare")
    assert (config.daily_trigger_time, config.room_sensor_refs, config.battery_refs) == ("12:12", (SENSOR,), ())
    assert config.local_safety == resolve_local_safety("viessmann_vicare", "Heizkoerper")
    assert (config.local_check_interval, config.telemetry_interval) == (
        options.DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS, options.DEFAULT_TELEMETRY_INTERVAL_SECONDS)
    assert config.abo_source is source and config.entitlement_path == Path("/tmp/e.json")


def test_test_intervals_come_from_the_document():
    config = dc.runtime_config({**KONF, "intervalle": {"pruefung_s": 5, "telemetrie_s": 10}},
                               entitlement_path=Path("/tmp/e.json"), abo_source=lambda: entitlement.ACTIVE)
    assert (config.local_check_interval, config.telemetry_interval) == (5, 10)


@pytest.mark.parametrize("konfiguration", [None, {"anlage": None}, {**KONF, "verteilsystem": "Kachelofen"},
                                           {**KONF, "tagestick": None}])
def test_unusable_documents_are_config_errors(konfiguration):
    with pytest.raises(ConfigError):
        dc.runtime_config(konfiguration, entitlement_path=Path("/tmp/e.json"), abo_source=lambda: entitlement.ACTIVE)


@pytest.mark.parametrize("abo, status", [
    ("aktiv", entitlement.ACTIVE), ("inaktiv", entitlement.INACTIVE), (None, entitlement.UNKNOWN),
    ("vielleicht", entitlement.UNKNOWN),
])
def test_abo_status(abo, status):
    assert dc.abo_status({**KONF, "abo": abo}) == status


def test_abo_of_an_empty_or_missing_configuration_is_unknown():
    assert dc.abo_status(None) == dc.abo_status({"anlage": None, "abo": "aktiv"}) == entitlement.UNKNOWN


@pytest.mark.parametrize("change, relevant", [
    ({"software": {"version": "0.6.1"}}, False), ({"mindest_software": "0.6.1"}, False), ({"profil": "x"}, False),
    ({"abo": "inaktiv"}, True), ({"raumfuehler": ["treiber:room_temperature"]}, True), ({"tagestick": "06:00"}, True),
    ({"intervalle": {"pruefung_s": 5}}, True), ({"anlage": None}, True),
])
def test_runtime_relevant_changes(change, relevant):
    assert dc.laufzeit_relevant(KONF, {**KONF, **change}) is relevant
    assert dc.laufzeit_relevant(None, KONF) is True


def test_query_uses_the_abo_source_and_never_asks_the_server(monkeypatch):
    def no_request(*args, **kwargs):
        raise AssertionError("kein Abruf im Geraete-Pfad")

    monkeypatch.setattr(entitlement, "query_status", no_request)
    config = dc.runtime_config(KONF, entitlement_path=Path("/tmp/e.json"), abo_source=lambda: entitlement.INACTIVE)
    assert entitlement.query(config) == entitlement.INACTIVE


def test_query_without_token_or_base_url_is_unknown(monkeypatch):
    from fakes import runtime_config

    monkeypatch.setattr(entitlement, "query_status", lambda *args, **kwargs: entitlement.ACTIVE)
    assert entitlement.query(runtime_config(accounts_api_base_url=None)) == entitlement.UNKNOWN
    assert entitlement.query(runtime_config()) == entitlement.ACTIVE


def test_the_own_mqtt_client_needs_a_transport(clock):
    config = dc.runtime_config(KONF, entitlement_path=Path("/tmp/e.json"), abo_source=lambda: entitlement.ACTIVE)
    with pytest.raises(ValueError, match="mqtt_factory"):
        mqtt_link.create_mqtt_client(config, RegulationWorker(clock=clock))
