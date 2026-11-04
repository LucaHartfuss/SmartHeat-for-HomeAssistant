"""Einmalige Migration der Rastung im Wartungsfenster: fehlende Datei und altes Ein-Paar-Format."""
import json
import stat

import konfig_form
import pytest
from device_helpers import _konfiguration, _leer
from geraet_helpers import gateway_konfiguration, ha_konfiguration

from smartheat_core import config_check, dateien, rastung
from smartheat_device import rastung_migration as migration
from smartheat_device.documents import DocumentStore

RELEASE = "1.4.0"
ALT = {"hebelsatz": "viessmann_vicare", "verteilsystem": "Heizkoerper", "seit": "2026-10-01T00:00:00+02:00"}


def _konfiguration_ablegen(ordner, inhalt, version=1):
    dateien.schreiben_json(ordner / "konfiguration.json", {"version": version, "inhalt": inhalt, "dirty": False},
                           modus=dateien.PRIVAT)


def _eingerichtet(ordner, **changes):
    """Gateway-Konfiguration wie nach der Einrichtung (Rastung dabei wieder entfernt: Stand vor der Rastung je Kreis)."""
    DocumentStore(ordner, host="gateway", software_version="0.6.0").empfangen("konfiguration",
                                                                               _konfiguration(**changes))
    (ordner / rastung.DATEI).unlink()


def _erwartet(**changes):
    return rastung.aus_konfiguration(_konfiguration(**changes))


def _alt_schreiben(ordner, **changes):
    (ordner / rastung.DATEI).write_text(json.dumps({**ALT, **changes}))


def _lauf(ordner, host="gateway", release=RELEASE):
    return migration.migrieren(ordner, host=host, release=release)


def _kreis(nummer):
    bindung = {"treiber": {"id": "simulation", "parameter": {"heizkreis": nummer}}, "thermostat": f"0x{100 + nummer:016x}"}
    return konfig_form.kreis({"bindung": bindung, "raumfuehler": [f"zigbee:0x{nummer:016x}:temperature"],
                              "hebelsatz": "viessmann_vicare", "verteilsystem": "Heizkoerper", "abo": "aktiv"},
                             nummer=nummer)


def _zwei_kreise():
    return gateway_konfiguration(kreise=[_kreis(1), _kreis(2)])


# --- Fall: Datei fehlt ----------------------------------------------------------------------------------------------

def test_a_missing_file_is_locked_from_the_confirmed_configuration(tmp_path):
    _eingerichtet(tmp_path)
    assert not (tmp_path / rastung.DATEI).exists()
    assert _lauf(tmp_path) == migration.AUS_KONFIGURATION
    assert rastung.lesen(tmp_path / rastung.DATEI) == _erwartet()
    assert stat.S_IMODE((tmp_path / rastung.DATEI).stat().st_mode) == 0o600
    assert not list(tmp_path.glob("*.pre-*"))          # nichts zu sichern: der Rueckweg ist das Loeschen der Datei


def test_a_missing_file_with_several_circles_locks_every_circle(tmp_path):
    _konfiguration_ablegen(tmp_path, _zwei_kreise())
    assert _lauf(tmp_path) == migration.AUS_KONFIGURATION
    gelesen = rastung.lesen(tmp_path / rastung.DATEI)
    assert [k.kreis for k in gelesen.kreise] == [1, 2] and gelesen == rastung.aus_konfiguration(_zwei_kreise())


def test_a_missing_file_is_locked_for_home_assistant_too(tmp_path):
    inhalt = ha_konfiguration()
    _konfiguration_ablegen(tmp_path, inhalt)
    assert _lauf(tmp_path, host="ha") == migration.AUS_KONFIGURATION
    gelesen = rastung.lesen(tmp_path / rastung.DATEI)
    assert gelesen == rastung.aus_konfiguration(inhalt) and gelesen.kreise[0].soll_entity is not None


def test_a_second_run_changes_nothing(tmp_path):
    _eingerichtet(tmp_path)
    assert _lauf(tmp_path) == migration.AUS_KONFIGURATION
    vorher = (tmp_path / rastung.DATEI).read_bytes()
    assert _lauf(tmp_path) == migration.BEREITS_AKTUELL
    assert (tmp_path / rastung.DATEI).read_bytes() == vorher and not list(tmp_path.glob("*.pre-*"))


def test_without_a_stored_configuration_nothing_is_written(tmp_path):
    assert _lauf(tmp_path) == migration.KONFIGURATION_NICHT_EINGERICHTET
    assert not (tmp_path / rastung.DATEI).exists()


def test_a_cleared_configuration_is_not_locked(tmp_path):
    _konfiguration_ablegen(tmp_path, {k: v for k, v in _leer().items() if k not in ("schema", "version")})
    assert _lauf(tmp_path) == migration.KONFIGURATION_NICHT_EINGERICHTET
    assert not (tmp_path / rastung.DATEI).exists()


def test_a_configuration_the_local_check_rejects_is_not_locked(tmp_path):
    inhalt = {k: v for k, v in _konfiguration().items() if k not in ("schema", "version")}
    inhalt["kreise"][0]["hebelsatz"] = "gibt_es_nicht"
    _konfiguration_ablegen(tmp_path, inhalt)
    assert _lauf(tmp_path) == migration.KONFIGURATION_UNGUELTIG
    assert not (tmp_path / rastung.DATEI).exists()


def test_a_failing_write_leaves_no_file(tmp_path, monkeypatch):
    _eingerichtet(tmp_path)

    def verweigert(*args, **kwargs):
        raise OSError("Datentraeger")

    monkeypatch.setattr(rastung.dateien, "schreiben_json", verweigert)
    assert _lauf(tmp_path) == migration.SCHREIBEN_FEHLGESCHLAGEN
    assert not (tmp_path / rastung.DATEI).exists()


# --- Fall: altes Ein-Paar-Format --------------------------------------------------------------------------------------

def test_the_old_format_becomes_a_list_with_one_circle_and_the_old_file_is_kept(tmp_path):
    _eingerichtet(tmp_path)
    _alt_schreiben(tmp_path)
    alt_bytes = (tmp_path / rastung.DATEI).read_bytes()
    assert _lauf(tmp_path) == migration.ALTES_FORMAT
    assert rastung.lesen(tmp_path / rastung.DATEI) == _erwartet()
    sicherung = tmp_path / f"{rastung.DATEI}.pre-{RELEASE}"
    assert sicherung.read_bytes() == alt_bytes and stat.S_IMODE(sicherung.stat().st_mode) == 0o600
    assert _lauf(tmp_path) == migration.BEREITS_AKTUELL
    assert sicherung.read_bytes() == alt_bytes


def test_the_way_back_over_the_backup_locks_loudly_again(tmp_path):
    """Rueckweg: .pre zurueckspielen, der alte Stand liest das alte Format; der neue sperrt es mit eigenem Code."""
    _eingerichtet(tmp_path)
    _alt_schreiben(tmp_path)
    assert _lauf(tmp_path) == migration.ALTES_FORMAT
    (tmp_path / f"{rastung.DATEI}.pre-{RELEASE}").replace(tmp_path / rastung.DATEI)
    assert json.loads((tmp_path / rastung.DATEI).read_text()) == ALT
    assert rastung.lesen(tmp_path / rastung.DATEI).sperre == "altes_format"
    store = DocumentStore(tmp_path, host="gateway", software_version="0.6.0")
    assert store.empfangen("konfiguration", _konfiguration(version=2)).grund == "hebelsatz_eingerastet"


@pytest.mark.parametrize("alt, code", [
    ({"verteilsystem": "Fussbodenheizung"}, migration.ALTES_FORMAT_PASST_NICHT),
    ({"hebelsatz": "vaillant_vrc720"}, migration.ALTES_FORMAT_PASST_NICHT),
    ({"hebelsatz": "gibt_es_nicht"}, migration.RASTUNG_GESPERRT),
])
def test_an_old_file_that_does_not_fit_the_configuration_is_left_alone(tmp_path, alt, code):
    _eingerichtet(tmp_path)
    _alt_schreiben(tmp_path, **alt)
    vorher = (tmp_path / rastung.DATEI).read_bytes()
    assert _lauf(tmp_path) == code
    assert (tmp_path / rastung.DATEI).read_bytes() == vorher and not list(tmp_path.glob("*.pre-*"))


def test_an_old_file_with_several_circles_in_the_configuration_is_left_alone(tmp_path):
    """Ein Paar sagt nichts ueber den Anschluss mehrerer Kreise: nie raten."""
    _konfiguration_ablegen(tmp_path, _zwei_kreise())
    _alt_schreiben(tmp_path)
    vorher = (tmp_path / rastung.DATEI).read_bytes()
    assert _lauf(tmp_path) == migration.ALTES_FORMAT_MEHRERE_KREISE
    assert (tmp_path / rastung.DATEI).read_bytes() == vorher and not list(tmp_path.glob("*.pre-*"))


def test_an_old_file_without_an_set_up_configuration_is_left_alone(tmp_path):
    _alt_schreiben(tmp_path)
    vorher = (tmp_path / rastung.DATEI).read_bytes()
    assert _lauf(tmp_path) == migration.KONFIGURATION_NICHT_EINGERICHTET
    assert (tmp_path / rastung.DATEI).read_bytes() == vorher


def test_an_existing_backup_is_never_overwritten(tmp_path):
    _eingerichtet(tmp_path)
    _alt_schreiben(tmp_path)
    sicherung = tmp_path / f"{rastung.DATEI}.pre-{RELEASE}"
    sicherung.write_text("frueher")
    vorher = (tmp_path / rastung.DATEI).read_bytes()
    assert _lauf(tmp_path) == migration.SICHERUNG_VORHANDEN
    assert sicherung.read_text() == "frueher" and (tmp_path / rastung.DATEI).read_bytes() == vorher


def test_a_dangling_backup_symlink_is_not_followed(tmp_path):
    _eingerichtet(tmp_path)
    _alt_schreiben(tmp_path)
    ziel = tmp_path / "ziel.txt"
    (tmp_path / f"{rastung.DATEI}.pre-{RELEASE}").symlink_to(ziel)
    assert _lauf(tmp_path) == migration.SICHERUNG_VORHANDEN
    assert not ziel.exists() and json.loads((tmp_path / rastung.DATEI).read_text()) == ALT


# --- Nie entsperren ---------------------------------------------------------------------------------------------------

def test_a_valid_new_file_is_current_and_untouched(tmp_path):
    _eingerichtet(tmp_path)
    rastung.schreiben(tmp_path / rastung.DATEI, rastung.aus_konfiguration(_zwei_kreise()), "2026-10-14T00:00:00+00:00")
    vorher = (tmp_path / rastung.DATEI).read_bytes()
    assert _lauf(tmp_path) == migration.BEREITS_AKTUELL
    assert (tmp_path / rastung.DATEI).read_bytes() == vorher


@pytest.mark.parametrize("inhalt", ["kaputt{", "[]", '{"kreise": []}', '{"hebelsatz": "viessmann_vicare"}'])
def test_a_broken_file_is_never_unlocked(tmp_path, inhalt):
    _eingerichtet(tmp_path)
    (tmp_path / rastung.DATEI).write_text(inhalt)
    assert _lauf(tmp_path) == migration.RASTUNG_GESPERRT
    assert (tmp_path / rastung.DATEI).read_text() == inhalt and not list(tmp_path.glob("*.pre-*"))


def test_a_broken_file_is_reported_as_locked_even_without_a_configuration(tmp_path):
    (tmp_path / rastung.DATEI).write_text("kaputt{")
    assert _lauf(tmp_path) == migration.RASTUNG_GESPERRT


def test_a_symlinked_file_is_never_unlocked_or_followed(tmp_path):
    _eingerichtet(tmp_path)
    ziel = tmp_path / "fremd.json"
    ziel.write_text(json.dumps(ALT))
    (tmp_path / rastung.DATEI).symlink_to(ziel)
    assert _lauf(tmp_path) == migration.RASTUNG_GESPERRT
    assert (tmp_path / rastung.DATEI).is_symlink() and json.loads(ziel.read_text()) == ALT


def test_a_dangling_symlink_is_not_a_missing_file(tmp_path):
    _eingerichtet(tmp_path)
    ziel = tmp_path / "ziel.json"
    (tmp_path / rastung.DATEI).symlink_to(ziel)
    assert _lauf(tmp_path) == migration.RASTUNG_GESPERRT
    assert not ziel.exists()


# --- Aufruf -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("release", ["", "../x", "a b", "a/b", "x" * 33, "1.4.0\n", "ä"])
def test_an_invalid_release_writes_nothing(tmp_path, release):
    _eingerichtet(tmp_path)
    _alt_schreiben(tmp_path)
    vorher = (tmp_path / rastung.DATEI).read_bytes()
    assert _lauf(tmp_path, release=release) == migration.RELEASE_UNGUELTIG
    assert (tmp_path / rastung.DATEI).read_bytes() == vorher and sorted(p.name for p in tmp_path.iterdir()) \
        == sorted([rastung.DATEI, "konfiguration.json", "dokumente.json"] if (tmp_path / "dokumente.json").exists()
                  else [rastung.DATEI, "konfiguration.json"])


@pytest.mark.parametrize("release", ["1.4.0", "heizungsbruecke-v1.4.0", "a_B-9.x", "x" * 32])
def test_valid_releases_are_accepted(tmp_path, release):
    _eingerichtet(tmp_path)
    assert _lauf(tmp_path, release=release) == migration.AUS_KONFIGURATION


def test_an_unknown_host_writes_nothing(tmp_path):
    _eingerichtet(tmp_path)
    assert _lauf(tmp_path, host="server") == migration.HOST_UNBEKANNT
    assert not (tmp_path / rastung.DATEI).exists()


def test_a_missing_folder_is_reported_and_not_created(tmp_path):
    assert _lauf(tmp_path / "device") == migration.ORDNER_FEHLT
    assert not (tmp_path / "device").exists()


def test_the_command_line_prints_one_code_without_names_and_sets_the_exit_code(tmp_path, capsys):
    _eingerichtet(tmp_path)
    assert migration.main(["--device-dir", str(tmp_path), "--host", "gateway", "--release", RELEASE]) == 0
    ausgabe = capsys.readouterr()
    assert ausgabe.out.strip() == migration.AUS_KONFIGURATION and ausgabe.err == ""
    assert "kunde3" not in ausgabe.out
    assert migration.main(["--device-dir", str(tmp_path), "--host", "gateway", "--release", "../x"]) == 1
    assert capsys.readouterr().out.strip() == migration.RELEASE_UNGUELTIG


def test_every_code_is_a_plain_lowercase_word_and_only_success_codes_exit_zero():
    codes = [wert for name, wert in vars(migration).items()
             if name.isupper() and not name.startswith("_") and isinstance(wert, str)]
    assert len(codes) == 14 and all(code.replace("_", "").isalpha() and code == code.lower() for code in codes)
    assert set(migration.ERFOLG) == {migration.AUS_KONFIGURATION, migration.ALTES_FORMAT, migration.BEREITS_AKTUELL}
    assert config_check.HOST_GATEWAY in migration.HOSTS and config_check.HOST_HA in migration.HOSTS


# --- Aktiver Boost --------------------------------------------------------------------------------------------------

def _boost_anlage(tmp_path, host, boost, *, ort="alt"):
    """Ein Geraet mit bestaetigter Konfiguration, ohne Rastung; der Zustand der Anlage (backup.json) liegt am alten Ort des
    Hosts oder im Kreisordner. Rueckgabe: der Geraeteordner."""
    ordner = tmp_path / "device"
    ordner.mkdir()
    _konfiguration_ablegen(ordner, ha_konfiguration() if host == "ha" else gateway_konfiguration())
    if ort == "alt":
        backup = tmp_path / "backup.json" if host == "ha" else tmp_path / "runtime" / "backup.json"
    else:
        backup = tmp_path / "runtime" / "kreis-2" / "backup.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps({"tenant_id": "kunde3", "lifetime_writes": 5, **boost}))
    return ordner


def _stand(daten):
    return {str(pfad.relative_to(daten)): pfad.read_bytes() for pfad in sorted(daten.rglob("*")) if pfad.is_file()}


@pytest.mark.parametrize("host", ["gateway", "ha"])
@pytest.mark.parametrize("ort", ["alt", "kreisordner"])
@pytest.mark.parametrize("boost", [{"boost_active": True}, {"emergency_boost_active": True}],
                         ids=["komfort-boost", "notfall-boost"])
def test_an_active_boost_refuses_the_migration_before_any_change(tmp_path, host, ort, boost):
    """Danach wartet das Geraet still auf das Dokument des Servers, ohne lokale Sicherheit: lief ein Boost, bliebe die
    Anlage darauf stehen. Die Migration verweigert, bevor sie etwas aendert."""
    ordner = _boost_anlage(tmp_path, host, boost, ort=ort)
    vorher = _stand(tmp_path)

    assert _lauf(ordner, host=host) == migration.BOOST_AKTIV
    assert _stand(tmp_path) == vorher and not (ordner / rastung.DATEI).exists()


def test_an_active_boost_ends_the_run_with_a_failure_code(tmp_path, capsys):
    ordner = _boost_anlage(tmp_path, "gateway", {"boost_active": True})
    assert migration.main(["--device-dir", str(ordner), "--host", "gateway", "--release", RELEASE]) == 1
    assert capsys.readouterr().out.strip() == "boost_aktiv"


@pytest.mark.parametrize("host", ["gateway", "ha"])
def test_without_an_active_boost_the_migration_runs_through(tmp_path, host):
    ordner = _boost_anlage(tmp_path, host, {"boost_active": False, "emergency_boost_active": False})
    assert _lauf(ordner, host=host) == migration.AUS_KONFIGURATION
    assert (ordner / rastung.DATEI).exists()


def test_the_boost_check_answers_with_a_plain_bool(tmp_path):
    assert migration.boost_aktiv(tmp_path, "gateway") is False   # ohne Datei kein Boost
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "backup.json").write_text(json.dumps({"boost_active": True}))
    assert migration.boost_aktiv(tmp_path, "gateway") is True
