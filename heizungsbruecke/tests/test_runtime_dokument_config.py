"""Spec 5b 5.2: Laufzeit aus dem Konfigurationsdokument (Plan D1 Task 9): BootInfo, RuntimeConfig ohne Transport,
Abo aus dem Dokument, Neustart nur bei laufzeitrelevanten Aenderungen."""
import dataclasses
import json
from pathlib import Path

import pytest
from bindung_beispiele import parameter

from smartheat_core import rastung
from smartheat_core.config_check import INTERVALLE
from smartheat_core.safety import LOCAL_SAFETY, resolve_local_safety
from smartheat_device import wire
from smartheat_runtime import dokument_config as dc
from smartheat_runtime import entitlement
from smartheat_runtime.dokument_config import ConfigError

SENSOR = "zigbee:0x00124b0000000001:temperature"
KONF = {
    "anlage": "kunde3", "profil": "viessmann_gastherme_heizkoerper", "hebelsatz": "viessmann_vicare",
    "verteilsystem": "Heizkoerper", "bindung": {"treiber": {"id": "simulation", "parameter": {}}, "thermostat": None},
    "raumfuehler": [SENSOR], "tagestick": "12:12", "abo": "aktiv", "mindest_software": "0.6.0", "software": None,
}
#: Mit Bindungsparametern (die Laufzeit-Tests oben brauchen sie nicht; ein Wechsel auf None waere dort nicht relevant).
KONF_MIT = {**KONF, "bindungsparameter": parameter("viessmann_vicare")}


def _boot(konfiguration, tmp_path, abgemeldet=False):
    return dc.boot_info(konfiguration, abgemeldet=abgemeldet, client_version="0.6.0", backup_path=tmp_path / "b.json",
                        failsafe_path=tmp_path / "f.json")


def test_fields_are_contract_fields():
    assert set(dc.FELDER) <= set(wire.KONFIGURATION_FIELDS) | set(wire.KONFIGURATION_OPTIONAL)
    assert set(dc.LAUFZEIT_FELDER) <= set(dc.FELDER)


def test_boot_info_of_a_set_up_device(tmp_path):
    boot = _boot(KONF, tmp_path)
    assert (boot.configured, boot.signed_off, boot.tenant_id) == (True, False, "kunde3")
    assert (boot.lever_set.id, boot.client_version, boot.notify_hints_off) == ("viessmann_vicare", "0.6.0", ())
    assert boot.local_check_interval == dc.DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS
    assert (boot.backup_path, boot.failsafe_path) == (tmp_path / "b.json", tmp_path / "f.json")


def test_boot_info_not_set_up_and_signed_off(tmp_path):
    assert (_boot(None, tmp_path).configured, _boot(None, tmp_path).signed_off) == (False, False)
    assert (_boot({"anlage": None}, tmp_path).configured, _boot({"anlage": None}, tmp_path).tenant_id) == (False, None)
    signed_off = _boot(KONF, tmp_path, abgemeldet=True)
    assert (signed_off.configured, signed_off.signed_off, signed_off.tenant_id) == (False, True, "kunde3")


def test_boot_info_never_throws(tmp_path):
    boot = _boot({"anlage": 5, "hebelsatz": "unbekannt", "intervalle": "x"}, tmp_path)
    assert boot.configured is False and boot.lever_set.id == "vaillant_vrc720"


def test_runtime_config_from_the_document(tmp_path):
    def source():
        return entitlement.ACTIVE

    config = dc.runtime_config(KONF, rastung_pfad=tmp_path / "keine.json", entitlement_path=Path("/tmp/e.json"),
                               abo_source=source)
    assert (config.tenant_id, config.lever_set_id) == ("kunde3", "viessmann_vicare")
    assert (config.daily_trigger_time, config.room_sensor_refs, config.battery_refs) == ("12:12", (SENSOR,), ())
    assert config.local_safety == resolve_local_safety("viessmann_vicare", "Heizkoerper")
    assert (config.local_check_interval, config.telemetry_interval) == (
        dc.DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS, dc.DEFAULT_TELEMETRY_INTERVAL_SECONDS)
    assert config.abo_source is source and config.entitlement_path == Path("/tmp/e.json")


PRUEFUNG_STANDARD, TELEMETRIE_STANDARD = dc.DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS, dc.DEFAULT_TELEMETRY_INTERVAL_SECONDS


def _intervalle(tmp_path, konfiguration=KONF):
    config = dc.runtime_config(konfiguration, rastung_pfad=tmp_path / "keine.json",
                               entitlement_path=Path("/tmp/e.json"), abo_source=lambda: entitlement.ACTIVE)
    return config.local_check_interval, config.telemetry_interval


@pytest.mark.parametrize(("roh", "erwartet"), [
    (None, (PRUEFUNG_STANDARD, TELEMETRIE_STANDARD)),
    ('{"pruefung_s": 5, "telemetrie_s": 10}', (5, 10)),
    ('{"pruefung_s": 0}', (PRUEFUNG_STANDARD, TELEMETRIE_STANDARD)),  # unter der Grenze
    ('{"telemetrie_s": 999999}', (PRUEFUNG_STANDARD, TELEMETRIE_STANDARD)),  # ueber der Grenze
    ('[1, 2]', (PRUEFUNG_STANDARD, TELEMETRIE_STANDARD)), ('kaputt', (PRUEFUNG_STANDARD, TELEMETRIE_STANDARD)),
])
def test_intervalle_nur_aus_der_e2e_umgebung_mit_den_grenzen(monkeypatch, tmp_path, roh, erwartet):
    """Plan S8a-1 P2, Review Focus 3: SMARTHEAT_E2E_INTERVALLE (JSON-Objekt) in den Grenzen von config_check.INTERVALLE;
    sonst gelten die Standardwerte, ohne Teiluebernahme, und kein Start scheitert."""
    if roh is None:
        monkeypatch.delenv(dc.E2E_INTERVALLE_ENV, raising=False)
    else:
        monkeypatch.setenv(dc.E2E_INTERVALLE_ENV, roh)
    assert _intervalle(tmp_path) == erwartet


@pytest.mark.parametrize("roh", ['{"pruefung_s": 5, "telemetrie_s": 0}', '{"pruefung_s": 5, "anders": 1}',
                                 '{"pruefung_s": true}', '{"pruefung_s": "5"}', '{"pruefung_s": NaN}',
                                 '{"telemetrie_s": Infinity}', '"5"', pytest.param("[" * 100_000, id="zu_tief"), ""])
def test_ein_ungueltiges_e2e_objekt_gilt_ganz_nicht(monkeypatch, tmp_path, caplog, roh):
    monkeypatch.setenv(dc.E2E_INTERVALLE_ENV, roh)
    assert _intervalle(tmp_path) == (PRUEFUNG_STANDARD, TELEMETRIE_STANDARD)
    assert roh == "" or (dc.E2E_INTERVALLE_ENV in caplog.text and roh not in caplog.text)  # Log ohne Wert


def test_ein_ungueltiger_wert_wird_je_aufruf_nur_einmal_geloggt(monkeypatch, tmp_path, caplog):
    """Review-Minor S8a-1: die Umgebungsvariable wird je runtime_config bzw. boot_info einmal gelesen, nicht je Feld."""
    monkeypatch.setenv(dc.E2E_INTERVALLE_ENV, '{"pruefung_s": 0}')

    def warnungen(aufruf):
        caplog.clear()
        aufruf()
        return [r for r in caplog.records if dc.E2E_INTERVALLE_ENV in r.getMessage()]

    assert len(warnungen(lambda: _boot(KONF, tmp_path))) == 1
    assert len(warnungen(lambda: _intervalle(tmp_path))) == 1


def test_intervalle_im_dokument_wirken_nicht_mehr(monkeypatch, tmp_path):
    """Plan S8a-1 P2: intervalle ist kein Dokumentfeld mehr; ein Server, der es noch schickt, aendert nichts."""
    monkeypatch.delenv(dc.E2E_INTERVALLE_ENV, raising=False)
    assert _intervalle(tmp_path, {**KONF, "intervalle": {"pruefung_s": 5, "telemetrie_s": 10}}) == (
        PRUEFUNG_STANDARD, TELEMETRIE_STANDARD)
    assert "intervalle" not in dc.FELDER + dc.LAUFZEIT_FELDER


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
    ({"intervalle": {"pruefung_s": 5}}, False), ({"anlage": None}, True),
    ({"bindungsparameter": {"wartezeit_s": 240}}, True), ({"bindungsparameter": None}, False),
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


def test_ursprungswerte_of_the_document():
    assert dc.ursprungswerte({**KONF, "ursprungswerte": {"heat_limit": 16.0}}) == {"heat_limit": 16.0}
    for ohne in (KONF, None, "x", {**KONF, "ursprungswerte": [1]}, {**KONF, "ursprungswerte": None}):
        assert dc.ursprungswerte(ohne) == {}


def test_arriving_originals_do_not_restart_the_runtime():
    """Spec 5.2: ursprungswerte steht nicht in LAUFZEIT_FELDER, ihr Eintreffen startet die Laufzeit nicht neu."""
    assert "ursprungswerte" not in dc.LAUFZEIT_FELDER
    assert dc.laufzeit_relevant(KONF, {**KONF, "ursprungswerte": {"heat_limit": 16.0}}) is False


def test_beschreibung_aus_hebelsatz_und_bindungsparametern():
    assert dc.beschreibung(KONF_MIT).lever_set.id == KONF_MIT["hebelsatz"]


def test_beschreibung_ohne_bindungsparameter_ist_ein_konfigurationsfehler():
    with pytest.raises(ConfigError):
        dc.beschreibung({**KONF_MIT, "bindungsparameter": None})


@pytest.mark.parametrize("hebelsatz", ["gibt_es_nicht", None, 5])
def test_unbekannter_hebelsatz_ist_ein_konfigurationsfehler(hebelsatz):
    with pytest.raises(ConfigError, match="Hebelsatz"):
        dc.beschreibung({**KONF, "hebelsatz": hebelsatz})
    with pytest.raises(ConfigError, match="Hebelsatz"):
        dc.sicherheit({**KONF, "hebelsatz": hebelsatz})


def test_sicherheit_ist_der_satz_von_hebelsatz_und_verteilsystem():
    assert dc.sicherheit(KONF) == resolve_local_safety("viessmann_vicare", "Heizkoerper")


def test_sicherheit_mit_unbekanntem_verteilsystem_ist_ein_konfigurationsfehler():
    with pytest.raises(ConfigError, match="Konfiguration"):
        dc.sicherheit({**KONF, "verteilsystem": "Kachelofen"})


@pytest.mark.parametrize("key", sorted(INTERVALLE))
def test_jedes_angenommene_intervall_startet(key, monkeypatch, tmp_path):
    """Spec Stufe 3 S5 3.2: die Grenzen stehen nur in config_check; die Laufzeit uebernimmt jeden Wert an beiden
    Grenzen (seit Plan S8a-1 P2 aus SMARTHEAT_E2E_INTERVALLE), einen knapp ausserhalb nicht."""
    low, high = INTERVALLE[key]
    feld = {"pruefung_s": "local_check_interval", "telemetrie_s": "telemetry_interval"}[key]
    standard = {"pruefung_s": PRUEFUNG_STANDARD, "telemetrie_s": TELEMETRIE_STANDARD}[key]
    for wert, erwartet in ((low, low), (high, high), (low - 1, standard), (high + 1, standard)):
        monkeypatch.setenv(dc.E2E_INTERVALLE_ENV, json.dumps({key: wert}))
        runtime = dc.runtime_config(KONF, rastung_pfad=tmp_path / "keine", entitlement_path=tmp_path / "e",
                                    abo_source=lambda: entitlement.ACTIVE)
        assert getattr(runtime, feld) == erwartet, wert


# --- Sicherheitswerte und Bindungsparameter aus dem Dokument (frueher in test_config.py des HA-Hosts) ---

VAILLANT = {"hebelsatz": "vaillant_vrc720", "verteilsystem": "Heizkoerper"}


def test_boost_values_outside_the_ranges_are_a_configuration_error(monkeypatch):
    """Boost-Werte ausserhalb der Bereiche sind ein Startfehler statt still geclampt (der Boost ist der einzige
    Schreibpfad ohne Server-Aufsicht). Geprueft wird in smartheat_core.safety.check_invariants (test_core_safety::
    test_invariants); hier der Weg bis zum Konfigurationsfehler."""
    safety = dict(LOCAL_SAFETY)
    heizkoerper = safety[("vaillant_vrc720", "Heizkoerper")]
    for comfort_boost in ({**heizkoerper.comfort_boost, "curve": 99.0}, {**heizkoerper.comfort_boost, "curve": -1.0},
                          {**heizkoerper.comfort_boost, "room_setpoint": 999.0},
                          {**heizkoerper.comfort_boost, "room_setpoint": 26.0}):
        safety[("vaillant_vrc720", "Heizkoerper")] = dataclasses.replace(heizkoerper, comfort_boost=comfort_boost)
        monkeypatch.setattr("smartheat_core.safety.LOCAL_SAFETY", safety)
        with pytest.raises(ConfigError, match="Comfort-Boost"):
            dc.sicherheit(VAILLANT)


def test_boost_values_on_the_range_boundaries_are_accepted(monkeypatch):
    safety = dict(LOCAL_SAFETY)
    heizkoerper = safety[("vaillant_vrc720", "Heizkoerper")]
    low, high = heizkoerper.ranges["curve"]
    for curve in (low, high):
        boosted = dataclasses.replace(heizkoerper, comfort_boost={**heizkoerper.comfort_boost, "curve": curve})
        safety[("vaillant_vrc720", "Heizkoerper")] = boosted
        monkeypatch.setattr("smartheat_core.safety.LOCAL_SAFETY", safety)
        assert dc.sicherheit(VAILLANT).comfort_boost["curve"] == curve


def test_the_document_carries_the_local_safety_values():
    safety = dc.sicherheit(VAILLANT)
    assert safety.ranges["curve"] == (0.4, 1.5)
    assert safety.ranges["room_setpoint"] == (15.0, 25.0)
    assert safety.ranges["min_flow"] == (20.0, 30.0)
    assert safety.ranges["heat_limit"] == (5.0, 23.0)
    assert safety.arrival_threshold_k == 0.5
    assert (safety.comfort_boost["curve"], safety.comfort_boost["room_setpoint"]) == (1.5, 25.0)
    assert safety.comfort_boost["heat_limit"] == 23.0  # bis 0.29.0: Boost-Heizgrenze = heat_limit_max


def test_weishaupt_has_its_own_safety_values():
    weishaupt = {"hebelsatz": "weishaupt_wwp", "verteilsystem": "Heizkoerper"}
    assert dc.sicherheit(weishaupt).ranges == {
        "curve": (0.30, 1.00), "room_setpoint": (16.0, 25.0), "heat_limit": (5.0, 23.0),
    }
    assert dc.sicherheit({**weishaupt, "verteilsystem": "Fussbodenheizung"}).comfort_boost == {}


@pytest.mark.parametrize("verteilsystem", [None, "", "Deckenheizung", "Unbekannt"])
def test_an_unknown_verteilsystem_has_no_safety_values(verteilsystem):
    with pytest.raises(ConfigError, match="Verteilsystem"):
        dc.sicherheit({**VAILLANT, "verteilsystem": verteilsystem})


def test_beschreibung_uebernimmt_die_bindungsparameter():
    vaillant = {**VAILLANT, "bindungsparameter": parameter("vaillant_vrc720")}
    assert dc.beschreibung(vaillant).settle_seconds == 2100
    weishaupt = {"hebelsatz": "weishaupt_wwp", "bindungsparameter": parameter("weishaupt_wwp")}
    assert dc.beschreibung(weishaupt).settle_seconds == 120
    langsam = {**weishaupt, "bindungsparameter": parameter("weishaupt_wwp", wartezeit_s=240)}
    assert dc.beschreibung(langsam).settle_seconds == 240


@pytest.mark.parametrize("konfiguration", [
    {"hebelsatz": "vaillant_vrc720"}, {"hebelsatz": "vaillant_vrc720", "bindungsparameter": {}},
    {"hebelsatz": "weishaupt_wwp", "bindungsparameter": None},
    {"hebelsatz": "vaillant_vrc720", "bindungsparameter": parameter("vaillant_vrc720", wartezeit_s=10 ** 6)},
    {"hebelsatz": "viessmann_vicare", "bindungsparameter": parameter("vaillant_vrc720")},
])
def test_beschreibung_ohne_passende_bindungsparameter_ist_ein_konfigurationsfehler(konfiguration):
    with pytest.raises(ConfigError, match="Konfiguration"):
        dc.beschreibung(konfiguration)
