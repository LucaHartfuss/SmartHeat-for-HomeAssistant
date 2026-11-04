"""Die Migrationen im Wartungsfenster lesen die flache Konfiguration des Stands vor den Kreisen (Heizungsbruecke 1.1.0,
so liegt sie auf den Geraeten): Fixtures in tests/daten sind Dateien, wie der alte Dokumentspeicher sie schrieb. Der
Normalbetrieb liest die flache Form nie; die Rastung, die aus ihr entsteht, gibt nie mehr frei als ihre Ziele."""
import copy
import json
import shutil
import stat
from pathlib import Path

import konfig_form
import pytest
from bindung_beispiele import parameter
from device_helpers import FakeHost, World

from smartheat_core import config_check, dateien, kennung, rastung, zustandspfade
from smartheat_device import bootstrap, zustand_migration
from smartheat_device import rastung_migration as migration
from smartheat_device.documents import DocumentStore
from smartheat_runtime import dokument_config

DATEN = Path(__file__).parent / "daten"
HA_DATEI = DATEN / "konfiguration_1_1_0_ha.json"
GATEWAY_DATEI = DATEN / "konfiguration_1_1_0_gateway.json"
RELEASE = "1.4.0"
ALT_HA = {"hebelsatz": "vaillant_vrc720", "verteilsystem": "Heizkoerper", "seit": "2026-10-01T00:00:00+02:00"}
ALT_GATEWAY = {"hebelsatz": "viessmann_vicare", "verteilsystem": "Heizkoerper", "seit": "2026-10-01T00:00:00+02:00"}
FALL = {"ha": (HA_DATEI, ALT_HA), "gateway": (GATEWAY_DATEI, ALT_GATEWAY)}


def _flach(datei) -> dict:
    return json.loads(datei.read_text())["inhalt"]


def _ablegen(ordner, datei, aendern=None):
    """Die Fixture als konfiguration.json; `aendern` darf den flachen Inhalt vorher umbauen."""
    if aendern is None:
        shutil.copyfile(datei, ordner / "konfiguration.json")
        return
    roh = json.loads(datei.read_text())
    aendern(roh["inhalt"])
    (ordner / "konfiguration.json").write_text(json.dumps(roh))


@pytest.fixture(params=sorted(FALL))
def host(request):
    return request.param


def _neue_form(host):
    """Das Dokument, das der Server nach der Umstellung fuer dieselbe Anlage schickt (mit seinen neuen Parametern)."""
    flach = {feld: wert for feld, wert in _flach(FALL[host][0]).items() if feld != "profil"}
    flach["bindungsparameter"] = parameter(flach["hebelsatz"])
    flach["mindest_software"] = "0.6.0"    # die Version der Testwelt; die Fixture verlangt die damalige Add-on-Version
    return konfig_form.nach_form({"schema": 2, "version": 9, **flach})


# --- Rastung aus der flachen Konfiguration -------------------------------------------------------------------------

def test_a_missing_rastung_is_derived_from_the_flat_configuration_as_a_list_with_one_circle(tmp_path, host):
    _ablegen(tmp_path, FALL[host][0])
    assert migration.migrieren(tmp_path, host=host, release=RELEASE) == migration.AUS_KONFIGURATION
    gelesen = rastung.lesen(tmp_path / rastung.DATEI)
    assert gelesen is not None and len(gelesen.kreise) == 1 and gelesen.kreise[0].kreis == 1
    flach = _flach(FALL[host][0])
    assert (gelesen.kreise[0].hebelsatz, gelesen.kreise[0].verteilsystem) == (flach["hebelsatz"], flach["verteilsystem"])
    assert gelesen.kreise[0].ziele == rastung.ziele(flach["bindung"])
    assert gelesen.kreise[0].soll_entity == rastung.soll_entity(flach["bindung"])
    assert (host != "ha") or gelesen.kreise[0].soll_entity == "climate.wohnzimmer_thermostat::temperature"
    assert stat.S_IMODE((tmp_path / rastung.DATEI).stat().st_mode) == 0o600
    assert not (tmp_path / "konfiguration.json").exists()    # die flache Datei ist beiseite gestellt


def test_the_old_one_pair_file_of_the_live_state_becomes_the_list_and_is_backed_up(tmp_path, host):
    datei, alt = FALL[host]
    _ablegen(tmp_path, datei)
    (tmp_path / rastung.DATEI).write_text(json.dumps(alt))
    alt_bytes = (tmp_path / rastung.DATEI).read_bytes()
    assert migration.migrieren(tmp_path, host=host, release=RELEASE) == migration.ALTES_FORMAT
    assert len(rastung.lesen(tmp_path / rastung.DATEI).kreise) == 1
    assert (tmp_path / f"{rastung.DATEI}.pre-{RELEASE}").read_bytes() == alt_bytes
    assert migration.migrieren(tmp_path, host=host, release=RELEASE) == migration.BEREITS_AKTUELL


def test_the_old_pair_must_equal_the_pair_of_the_flat_configuration(tmp_path, host):
    datei, alt = FALL[host]
    _ablegen(tmp_path, datei)
    (tmp_path / rastung.DATEI).write_text(json.dumps({**alt, "verteilsystem": "Fussbodenheizung"}))
    vorher = (tmp_path / rastung.DATEI).read_bytes()
    assert migration.migrieren(tmp_path, host=host, release=RELEASE) == migration.ALTES_FORMAT_PASST_NICHT
    assert (tmp_path / rastung.DATEI).read_bytes() == vorher and not list(tmp_path.glob("*.pre-*"))


def test_the_derived_rastung_accepts_the_new_document_of_the_same_installation(tmp_path, host):
    _ablegen(tmp_path, FALL[host][0])
    assert migration.migrieren(tmp_path, host=host, release=RELEASE) == migration.AUS_KONFIGURATION
    neu = _neue_form(host)
    assert config_check.pruefen(neu, rastung.lesen(tmp_path / rastung.DATEI), host=host) is None


def test_the_derived_rastung_releases_nothing_beyond_the_described_targets(tmp_path, host):
    _ablegen(tmp_path, FALL[host][0])
    migration.migrieren(tmp_path, host=host, release=RELEASE)
    gelesen = rastung.lesen(tmp_path / rastung.DATEI)
    neu = _neue_form(host)
    if host == "ha":
        neu["kreise"][0]["bindung"]["rollen"]["curve"] = "number.fremde_kurve"
    else:
        neu["kreise"][0]["bindung"]["thermostat"] = "0x00124b00000000ff"
    assert config_check.pruefen(neu, gelesen, host=host).grund == config_check.HEBELSATZ_EINGERASTET
    zwei = _neue_form(host)
    zwei["kreise"].append({**copy.deepcopy(zwei["kreise"][0]), "kreis": 2})
    assert not rastung.passt(gelesen, zwei)                        # ein zweiter Kreis war nie beschrieben
    fremd = _neue_form(host)
    fremd["kreise"][0]["verteilsystem"] = "Fussbodenheizung"
    assert not rastung.passt(gelesen, fremd)


def test_a_flat_document_with_a_changed_pair_is_not_rasted(tmp_path, host):
    _ablegen(tmp_path, FALL[host][0], lambda inhalt: inhalt.update(hebelsatz="gibt_es_nicht"))
    assert migration.migrieren(tmp_path, host=host, release=RELEASE) == migration.KONFIGURATION_UNGUELTIG
    assert not (tmp_path / rastung.DATEI).exists()


@pytest.mark.parametrize("aendern", [
    lambda inhalt: inhalt.pop("raumfuehler"),
    lambda inhalt: inhalt.update(raumfuehler=[]),
    lambda inhalt: inhalt.update(raumfuehler="sensor.x"),
    lambda inhalt: inhalt.pop("bindung"),
    lambda inhalt: inhalt.update(bindung={"kreis": {"plattform": "unbekannt", "anlage": "A", "nummer": "0"}}),
    lambda inhalt: inhalt.update(fremdes_feld=1),
    lambda inhalt: inhalt.update(tagestick="25:99"),
    lambda inhalt: inhalt.update(abo="vielleicht"),
    lambda inhalt: inhalt.pop("bindungsparameter"),
    lambda inhalt: inhalt.update(kreise=[]),
])
def test_a_malformed_flat_document_is_never_rasted(tmp_path, aendern):
    _ablegen(tmp_path, HA_DATEI, aendern)
    assert migration.migrieren(tmp_path, host="ha", release=RELEASE) == migration.KONFIGURATION_UNGUELTIG
    assert not (tmp_path / rastung.DATEI).exists()


def test_a_flat_document_of_the_other_host_is_not_rasted(tmp_path):
    _ablegen(tmp_path, GATEWAY_DATEI)
    assert migration.migrieren(tmp_path, host="ha", release=RELEASE) == migration.KONFIGURATION_UNGUELTIG
    assert not (tmp_path / rastung.DATEI).exists()


def test_a_flat_document_of_the_ha_host_is_not_rasted_at_the_gateway(tmp_path):
    _ablegen(tmp_path, HA_DATEI)
    assert migration.migrieren(tmp_path, host="gateway", release=RELEASE) == migration.KONFIGURATION_UNGUELTIG
    assert not (tmp_path / rastung.DATEI).exists()


def test_a_flat_document_with_a_binding_that_is_no_object_is_not_rasted(tmp_path):
    _ablegen(tmp_path, HA_DATEI, lambda inhalt: inhalt.update(bindung=[]))
    assert migration.migrieren(tmp_path, host="ha", release=RELEASE) == migration.KONFIGURATION_UNGUELTIG
    assert not (tmp_path / rastung.DATEI).exists()


def test_a_flat_document_with_a_lever_set_the_way_does_not_carry_is_not_rasted(tmp_path):
    _ablegen(tmp_path, HA_DATEI, lambda inhalt: inhalt.update(hebelsatz="viessmann_vicare"))
    assert migration.migrieren(tmp_path, host="ha", release=RELEASE) == migration.KONFIGURATION_UNGUELTIG
    assert not (tmp_path / rastung.DATEI).exists()


def test_a_cleared_flat_configuration_is_not_rasted(tmp_path):
    _ablegen(tmp_path, HA_DATEI, lambda inhalt: inhalt.update(anlage=None))
    assert migration.migrieren(tmp_path, host="ha", release=RELEASE) == migration.KONFIGURATION_NICHT_EINGERICHTET
    assert not (tmp_path / rastung.DATEI).exists()


def test_the_restore_list_of_the_old_parameters_is_completed_by_the_local_minimum_only_in_memory(tmp_path):
    _ablegen(tmp_path, HA_DATEI)
    gespeichert = json.loads((tmp_path / "konfiguration.json").read_text())["inhalt"]["bindungsparameter"]
    assert gespeichert["zurueck"] == ["heat_limit"]                # so sendete der alte Server sie
    inhalt, code = migration.bestaetigte_konfiguration(tmp_path, "ha")
    assert code is None and inhalt is not None
    assert set(inhalt["bindungsparameter"]["zurueck"]) == {"curve", "room_setpoint", "heat_limit", "min_flow"}
    assert json.loads((tmp_path / "konfiguration.json").read_text())["inhalt"]["bindungsparameter"] == gespeichert


@pytest.mark.parametrize("aendern", [
    lambda parameter_: parameter_.update(zurueck=["heat_limit", "gibt_es_nicht"]),
    lambda parameter_: parameter_.update(zurueck="heat_limit"),
    lambda parameter_: parameter_["schritte"].pop("curve"),
    lambda parameter_: parameter_.update(wartezeit_s=1),
])
def test_other_faults_in_the_old_parameters_stay_faults(tmp_path, aendern):
    _ablegen(tmp_path, HA_DATEI, lambda inhalt: aendern(inhalt["bindungsparameter"]))
    assert migration.migrieren(tmp_path, host="ha", release=RELEASE) == migration.KONFIGURATION_UNGUELTIG
    assert not (tmp_path / rastung.DATEI).exists()


def test_the_confirmed_configuration_of_a_flat_file_has_the_form_with_circles(tmp_path, host):
    _ablegen(tmp_path, FALL[host][0])
    inhalt, code = migration.bestaetigte_konfiguration(tmp_path, host)
    assert code is None and inhalt is not None
    flach = _flach(FALL[host][0])
    assert [k["kreis"] for k in inhalt["kreise"]] == [1] and inhalt["kreise"][0]["bindung"] == flach["bindung"]
    assert config_check.pruefen(inhalt, None, host=host) is None
    assert inhalt["kreise"][0]["raeume"][0]["fuehler"] == flach["raumfuehler"]
    assert "profil" not in inhalt and "raumfuehler" not in inhalt
    assert inhalt["kreise"][0].get("ursprungswerte") == flach.get("ursprungswerte")
    kreis = inhalt["kreise"][0]
    assert kreis["kreis"] == 1 and kreis["raeume"] == [
        {"raum": 1, "name": "Raum 1", "rolle": "referenz", "fuehler": flach["raumfuehler"], "soll_quelle": False,
         "versatz_k": 0.0, "ziel_fest": None}]


# --- Normalbetrieb: keine Kompatibilitaet -------------------------------------------------------------------------

def test_the_normal_operation_never_reads_the_flat_form(tmp_path, host):
    flach = _flach(FALL[host][0])
    assert config_check.pruefen(flach, None, host=host).grund == config_check.UNGUELTIG
    assert rastung.aus_konfiguration(flach) is None
    assert not rastung.passt(rastung.aus_konfiguration(_neue_form(host)), flach)
    # Ein neuer Speicher raestet eine flache Konfiguration nicht ein und lehnt sie ab
    ergebnis = DocumentStore(tmp_path, host=host, software_version="1.4.0").empfangen(
        "konfiguration", {"schema": 2, "version": 1, **flach})
    assert not ergebnis.ok and not (tmp_path / rastung.DATEI).exists()


def test_a_new_store_with_a_flat_file_and_no_rastung_stays_locked(tmp_path, host):
    _ablegen(tmp_path, FALL[host][0])
    speicher = DocumentStore(tmp_path, host=host, software_version="1.4.0")
    ergebnis = speicher.empfangen("konfiguration", _neue_form(host))
    assert (ergebnis.ok, ergebnis.grund) == (False, config_check.HEBELSATZ_EINGERASTET)
    assert migration.migrieren(tmp_path, host=host, release=RELEASE) == migration.AUS_KONFIGURATION
    neu = DocumentStore(tmp_path, host=host, software_version="1.4.0").empfangen("konfiguration", _neue_form(host))
    assert neu.ok


# --- Zustand: dieselbe bestaetigte Konfiguration ---------------------------------------------------------------

def test_the_state_migration_finds_the_circle_in_the_flat_file_of_the_live_state(tmp_path):
    device = tmp_path / "device"
    device.mkdir()
    _ablegen(device, HA_DATEI)
    alt = json.dumps({"anlage": "Zuhause", "nummer": "0", "plattform": "mypyllant"}, sort_keys=True)
    dateien.schreiben_json(tmp_path / "backup.json", {"tenant_id": "zuhause", "plant_id": alt, "lifetime_writes": 5},
                           modus=dateien.PRIVAT)
    assert zustand_migration.migrieren(device, host="ha", release=RELEASE) == zustand_migration.MIGRIERT
    neu = json.loads((zustandspfade.kreis_dir(tmp_path / "runtime", 1) / "backup.json").read_text())
    assert neu["plant_id"] == kennung.aus_ha_kreis("mypyllant", "Zuhause", "0")


# --- Nach der Migration wartet das Geraet still auf das Dokument mit Kreisen -------------------------------------------

class _HaHost(FakeHost):
    host = "ha"


def _geraet_nach_migration(tmp_path, host):
    """Wartungsfenster wie im Ablaufbuch: Datei des Stands 1.1.0 und altes Ein-Paar-Format, Migration, dann startet das
    Geraet neu (neue Welt auf denselben Dateien; der Zugang bleibt gespeichert, das Geraet ist uebernommen)."""
    datei, alt = FALL[host]
    ordner = tmp_path / "device"
    ordner.mkdir()
    _ablegen(ordner, datei)
    (ordner / rastung.DATEI).write_text(json.dumps(alt))
    assert migration.migrieren(ordner, host=host, release=RELEASE) == migration.ALTES_FORMAT
    return ordner, World(tmp_path, _HaHost() if host == "ha" else FakeHost())


def test_the_flat_file_is_put_aside_as_a_backup_after_a_successful_migration(tmp_path, host):
    datei = FALL[host][0]
    ordner, _ = _geraet_nach_migration(tmp_path, host)
    assert not (ordner / "konfiguration.json").exists()
    sicherung = ordner / f"konfiguration.json.pre-{RELEASE}"
    assert sicherung.read_bytes() == datei.read_bytes() and stat.S_IMODE(sicherung.stat().st_mode) == 0o600


def test_the_device_after_the_migration_waits_quietly_for_the_document_with_circles(tmp_path, host):
    ordner, world = _geraet_nach_migration(tmp_path, host)
    assert not world.device.documents.eingerichtet() and world.device.documents.inhalt("konfiguration") is None
    assert dokument_config.eingerichtet(world.device.documents.inhalt("konfiguration") or {}) is False
    world.verbinden()
    anzeige = world.device.anzeige()
    assert (anzeige.zustand, anzeige.eingerichtet, anzeige.device_state) == (
        "wartet_auf_einrichtung", False, "uebernommen")
    assert anzeige.hinweise == () and anzeige.konfiguration_abgelehnt is None and not anzeige.code_zeigen
    assert world.device.documents.ablehnung("konfiguration") is None
    (hello,) = world.link.of("up/hello")
    assert hello["konfiguration_version"] == 0    # der Server schickt sein Dokument


def test_the_document_with_circles_then_sets_the_device_up_without_a_new_registration(tmp_path, host):
    ordner, world = _geraet_nach_migration(tmp_path, host)
    world.verbinden()
    kennung_vorher = world.device.identity.device_id
    neu = _neue_form(host)
    world.empfangen("down/config", neu)
    assert world.device.documents.eingerichtet() and world.device.documents.version("konfiguration") == 9
    assert world.device.documents.ablehnung("konfiguration") is None
    assert [alt for alt, _ in world.host.konfigurationen] == [None]    # Einrichtung, nichts abgeloest
    assert world.device.identity.device_id == kennung_vorher and bootstrap.device_state(ordner) == "uebernommen"
    assert world.client.calls == []                                     # keine neue Registrierung
    assert len(rastung.lesen(ordner / rastung.DATEI).kreise) == 1


def test_a_document_that_the_migrated_rastung_does_not_cover_is_still_refused(tmp_path, host):
    ordner, world = _geraet_nach_migration(tmp_path, host)
    world.verbinden()
    fremd = _neue_form(host)
    fremd["kreise"][0]["verteilsystem"] = "Fussbodenheizung"
    world.empfangen("down/config", fremd)
    assert not world.device.documents.eingerichtet()
    assert world.device.documents.ablehnung("konfiguration")[1] == config_check.HEBELSATZ_EINGERASTET


@pytest.mark.parametrize("code, aendern", [
    (migration.ALTES_FORMAT_PASST_NICHT, lambda alt: {**alt, "verteilsystem": "Fussbodenheizung"}),
    (migration.RASTUNG_GESPERRT, lambda alt: "kaputt"),
])
def test_without_success_the_flat_file_stays_where_it_is(tmp_path, host, code, aendern):
    datei, alt = FALL[host]
    _ablegen(tmp_path, datei)
    neu = aendern(alt)
    (tmp_path / rastung.DATEI).write_text(neu if isinstance(neu, str) else json.dumps(neu))
    assert migration.migrieren(tmp_path, host=host, release=RELEASE) == code
    assert (tmp_path / "konfiguration.json").read_bytes() == datei.read_bytes()
    assert not list(tmp_path.glob("konfiguration.json.pre-*"))


def test_an_existing_backup_of_the_flat_file_stops_the_run_before_any_change(tmp_path, host):
    _ablegen(tmp_path, FALL[host][0])
    (tmp_path / f"konfiguration.json.pre-{RELEASE}").write_text("alt")
    assert migration.migrieren(tmp_path, host=host, release=RELEASE) == migration.SICHERUNG_VORHANDEN
    assert (tmp_path / "konfiguration.json").read_bytes() == FALL[host][0].read_bytes()
    assert not (tmp_path / rastung.DATEI).exists()
    assert (tmp_path / f"konfiguration.json.pre-{RELEASE}").read_text() == "alt"


def test_a_rerun_after_a_failed_removal_puts_the_flat_file_aside(tmp_path, host):
    """Die Rastung steht schon, die Datei noch: der zweite Lauf holt das Beiseitestellen nach (Exit 0)."""
    _ablegen(tmp_path, FALL[host][0])
    inhalt, _ = migration.bestaetigte_konfiguration(tmp_path, host)
    rastung.schreiben(tmp_path / rastung.DATEI, rastung.aus_konfiguration(inhalt), "2026-11-01T00:00:00+01:00")
    assert migration.migrieren(tmp_path, host=host, release=RELEASE) == migration.BEREITS_AKTUELL
    assert not (tmp_path / "konfiguration.json").exists()
    assert (tmp_path / f"konfiguration.json.pre-{RELEASE}").read_bytes() == FALL[host][0].read_bytes()
    assert migration.migrieren(tmp_path, host=host, release=RELEASE) == migration.BEREITS_AKTUELL


def test_a_stored_document_with_circles_is_never_put_aside(tmp_path, host):
    speicher = DocumentStore(tmp_path, host=host, software_version="1.4.0")
    assert speicher.empfangen("konfiguration", _neue_form(host)).ok
    vorher = (tmp_path / "konfiguration.json").read_bytes()
    assert migration.migrieren(tmp_path, host=host, release=RELEASE) == migration.BEREITS_AKTUELL
    assert (tmp_path / "konfiguration.json").read_bytes() == vorher and not list(tmp_path.glob("*.pre-*"))


def test_the_state_migration_after_the_rastung_migration_reads_the_backup_of_the_flat_file(tmp_path):
    """Reihenfolge des Ablaufbuchs: erst Rastung (stellt die Datei beiseite), dann Zustand (braucht die Kennung)."""
    device = tmp_path / "device"
    device.mkdir()
    _ablegen(device, HA_DATEI)
    alt = json.dumps({"anlage": "Zuhause", "nummer": "0", "plattform": "mypyllant"}, sort_keys=True)
    dateien.schreiben_json(tmp_path / "backup.json", {"tenant_id": "zuhause", "plant_id": alt, "lifetime_writes": 5},
                           modus=dateien.PRIVAT)
    assert migration.migrieren(device, host="ha", release=RELEASE) == migration.AUS_KONFIGURATION
    assert not (device / "konfiguration.json").exists()
    assert zustand_migration.migrieren(device, host="ha", release=RELEASE) == zustand_migration.MIGRIERT
    neu = json.loads((zustandspfade.kreis_dir(tmp_path / "runtime", 1) / "backup.json").read_text())
    assert neu["plant_id"] == kennung.aus_ha_kreis("mypyllant", "Zuhause", "0")


def test_the_state_migration_without_any_configuration_still_stops(tmp_path):
    device = tmp_path / "device"
    device.mkdir()
    dateien.schreiben_json(tmp_path / "backup.json", {"tenant_id": "zuhause", "plant_id": "x", "lifetime_writes": 5},
                           modus=dateien.PRIVAT)
    assert zustand_migration.migrieren(device, host="ha", release=RELEASE) == zustand_migration.KONFIGURATION_NICHT_EINGERICHTET
    assert (tmp_path / "backup.json").exists()


def _rastung_ohne_beiseitestellen(ordner, host):
    inhalt, _ = migration.bestaetigte_konfiguration(ordner, host)
    rastung.schreiben(ordner / rastung.DATEI, rastung.aus_konfiguration(inhalt), "2026-11-01T00:00:00+01:00")


def test_a_rerun_with_an_existing_backup_does_not_report_success_and_keeps_both_files(tmp_path, host):
    _ablegen(tmp_path, FALL[host][0])
    _rastung_ohne_beiseitestellen(tmp_path, host)
    (tmp_path / f"konfiguration.json.pre-{RELEASE}").write_text("alt")
    assert migration.migrieren(tmp_path, host=host, release=RELEASE) == migration.SICHERUNG_VORHANDEN
    assert (tmp_path / "konfiguration.json").read_bytes() == FALL[host][0].read_bytes()
    assert (tmp_path / f"konfiguration.json.pre-{RELEASE}").read_text() == "alt"


def test_a_failing_write_of_the_backup_leaves_the_flat_file_and_reports_the_failure(tmp_path, host, monkeypatch):
    _ablegen(tmp_path, FALL[host][0])
    _rastung_ohne_beiseitestellen(tmp_path, host)

    def verweigert(*args, **kwargs):
        raise OSError("Datentraeger")

    monkeypatch.setattr(migration.dateien, "schreiben", verweigert)
    assert migration.migrieren(tmp_path, host=host, release=RELEASE) == migration.SCHREIBEN_FEHLGESCHLAGEN
    assert (tmp_path / "konfiguration.json").read_bytes() == FALL[host][0].read_bytes()
    assert not (tmp_path / f"konfiguration.json.pre-{RELEASE}").exists()
