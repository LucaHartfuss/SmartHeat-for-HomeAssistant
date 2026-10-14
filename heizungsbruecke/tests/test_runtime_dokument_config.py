"""Spec 5b 5.2: Laufzeit aus dem Konfigurationsdokument (Plan D1 Task 9): BootInfo, RuntimeConfig ohne Transport,
Abo aus dem Dokument, Neustart nur bei laufzeitrelevanten Aenderungen."""
from pathlib import Path

import pytest

from smartheat_core import rastung
from smartheat_core.safety import resolve_local_safety
from smartheat_device import wire
from smartheat_runtime import dokument_config as dc
from smartheat_runtime import entitlement, options
from smartheat_runtime.options import ConfigError

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


def test_runtime_config_from_the_document(tmp_path):
    def source():
        return entitlement.ACTIVE

    config = dc.runtime_config(KONF, rastung_pfad=tmp_path / "keine.json", entitlement_path=Path("/tmp/e.json"),
                               abo_source=source)
    assert (config.tenant_id, config.setup_id, config.lever_set_id) == ("kunde3", "kunde3", "viessmann_vicare")
    assert (config.daily_trigger_time, config.room_sensor_refs, config.battery_refs) == ("12:12", (SENSOR,), ())
    assert config.local_safety == resolve_local_safety("viessmann_vicare", "Heizkoerper")
    assert (config.local_check_interval, config.telemetry_interval) == (
        options.DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS, options.DEFAULT_TELEMETRY_INTERVAL_SECONDS)
    assert config.abo_source is source and config.entitlement_path == Path("/tmp/e.json")


def test_test_intervals_come_from_the_document(tmp_path):
    config = dc.runtime_config({**KONF, "intervalle": {"pruefung_s": 5, "telemetrie_s": 10}},
                               rastung_pfad=tmp_path / "keine.json", entitlement_path=Path("/tmp/e.json"), abo_source=lambda: entitlement.ACTIVE)
    assert (config.local_check_interval, config.telemetry_interval) == (5, 10)


@pytest.mark.parametrize("konfiguration", [None, {"anlage": None}, {**KONF, "verteilsystem": "Kachelofen"},
                                           {**KONF, "tagestick": None}])
def test_unusable_documents_are_config_errors(konfiguration, tmp_path):
    with pytest.raises(ConfigError):
        dc.runtime_config(konfiguration, rastung_pfad=tmp_path / "keine.json", entitlement_path=Path("/tmp/e.json"),
                          abo_source=lambda: entitlement.ACTIVE)


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


def test_query_uses_the_abo_source(tmp_path):
    config = dc.runtime_config(KONF, rastung_pfad=tmp_path / "keine.json", entitlement_path=Path("/tmp/e.json"),
                               abo_source=lambda: entitlement.INACTIVE)
    assert entitlement.query(config) == entitlement.INACTIVE


def test_the_sign_off_marker_binds_to_the_installation_and_is_private(tmp_path):
    pfad = tmp_path / "runtime" / "abgemeldet.json"
    assert not dc.abgemeldet(pfad, KONF) and not dc.abgemeldet(pfad, None)
    assert dc.abmelden(pfad, "kunde3") is True
    assert dc.abmelden(pfad, "kunde3") is False  # erneute Zustellung: nichts Neues
    assert dc.abgemeldet(pfad, KONF) and not dc.abgemeldet(pfad, {**KONF, "anlage": "andere"})
    assert not dc.abgemeldet(pfad, None) and pfad.stat().st_mode & 0o777 == 0o600
    assert dc.abmelden(pfad, "andere") is True and not dc.abgemeldet(pfad, KONF)


def test_runtime_refuses_a_configuration_that_does_not_fit_the_lock(tmp_path):
    pfad = tmp_path / "sicherheitssatz.json"
    rastung.schreiben(pfad, rastung.Rastung("viessmann_vicare", "Fussbodenheizung"), "2026-10-14T00:00:00+00:00")
    with pytest.raises(ConfigError, match="festgelegt"):
        dc.runtime_config(KONF, rastung_pfad=pfad, entitlement_path=tmp_path / "e.json", abo_source=lambda: "active")


def test_runtime_refuses_everything_with_a_broken_lock(tmp_path):
    (tmp_path / "sicherheitssatz.json").write_text("{")
    with pytest.raises(ConfigError):
        dc.runtime_config(KONF, rastung_pfad=tmp_path / "sicherheitssatz.json", entitlement_path=tmp_path / "e.json",
                          abo_source=lambda: "active")


def test_runtime_refuses_everything_with_a_lock_that_is_a_symlink(tmp_path):
    ziel = tmp_path / "ziel.json"
    rastung.schreiben(ziel, rastung.Rastung("viessmann_vicare", "Heizkoerper"), "2026-10-14T00:00:00+00:00")
    (tmp_path / "sicherheitssatz.json").symlink_to(ziel)
    with pytest.raises(ConfigError, match="festgelegt"):
        dc.runtime_config(KONF, rastung_pfad=tmp_path / "sicherheitssatz.json", entitlement_path=tmp_path / "e.json",
                          abo_source=lambda: "active")


def test_runtime_starts_without_lock_file_and_with_the_same_pair(tmp_path):
    assert dc.runtime_config(KONF, rastung_pfad=tmp_path / "fehlt.json", entitlement_path=tmp_path / "e.json",
                             abo_source=lambda: "active").tenant_id == "kunde3"
    pfad = tmp_path / "sicherheitssatz.json"
    rastung.schreiben(pfad, rastung.Rastung("viessmann_vicare", "Heizkoerper"), "2026-10-14T00:00:00+00:00")
    assert dc.runtime_config(KONF, rastung_pfad=pfad, entitlement_path=tmp_path / "e.json",
                             abo_source=lambda: "active").tenant_id == "kunde3"


def test_the_lock_check_never_throws_anything_but_config_error(tmp_path):
    pfad = tmp_path / "sicherheitssatz.json"
    rastung.schreiben(pfad, rastung.Rastung("viessmann_vicare", "Heizkoerper"), "2026-10-14T00:00:00+00:00")
    with pytest.raises(ConfigError):
        dc.rastung_pruefen(None, pfad)
    dc.rastung_pruefen(None, tmp_path / "fehlt.json")  # ohne Rastung nichts zu pruefen
