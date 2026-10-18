"""Spec 5b 3 und 5.1: Dokumentspeicher des Geraets und atomares Schreiben (Plan D1 Task 3)."""
import json
import math

import pytest
from bindung_beispiele import parameter

from smartheat_core import config_check, dateien, rastung
from smartheat_device import documents as docs
from smartheat_device import wire

SENSOR = "zigbee:0x00124b0000000001:temperature"


def _konfiguration(version=1, **changes):
    inhalt = {
        "anlage": "kunde3", "profil": "viessmann_gastherme_heizkoerper", "hebelsatz": "viessmann_vicare",
        "verteilsystem": "Heizkoerper",
        "bindung": {"treiber": {"id": "simulation", "parameter": {"heizkreis": 0}}, "thermostat": None},
        "raumfuehler": [SENSOR], "tagestick": "12:12", "abo": "aktiv", "mindest_software": "0.6.0", "software": None,
        "bindungsparameter": parameter("viessmann_vicare"),
    }
    inhalt.update(changes)
    return {"schema": 1, "version": version, **inhalt}


def _leer(version=1, mindest="0.6.0"):
    return {"schema": 1, "version": version, "anlage": None, "profil": None, "hebelsatz": None, "verteilsystem": None,
            "bindung": None, "raumfuehler": [], "tagestick": None, "abo": None, "mindest_software": mindest,
            "software": None, "bindungsparameter": None}


def _bedienung(version=1, raum_soll=21.0, **changes):
    return {"schema": 1, "version": version, "modus": "manuell", "raum_soll": raum_soll, "quelle": "portal",
            "ts": "2027-01-15T08:00:00+00:00", **changes}


def _store(tmp_path, version="0.6.0"):
    return docs.DocumentStore(tmp_path, host="gateway", software_version=version)


# --- Konfiguration ---

def test_a_empty_configuration_does_not_release_the_lock(tmp_path):
    """A5-06 (SI-1): leer und danach ein anderes Verteilsystem loeste das Einrasten. Jetzt bleibt die Rastung."""
    store = _store(tmp_path)
    store.empfangen("konfiguration", _konfiguration(version=1, verteilsystem="Fussbodenheizung"))
    assert store.empfangen("konfiguration", _leer(version=2)).uebernommen
    ergebnis = store.empfangen("konfiguration", _konfiguration(version=3, verteilsystem="Heizkoerper"))
    assert (ergebnis.ok, ergebnis.grund) == (False, "hebelsatz_eingerastet")
    assert _store(tmp_path).empfangen("konfiguration", _konfiguration(version=4, verteilsystem="Heizkoerper")).grund \
        == "hebelsatz_eingerastet"
    assert _store(tmp_path).empfangen("konfiguration", _konfiguration(version=5, verteilsystem="Fussbodenheizung")).ok


def test_the_first_set_up_configuration_creates_the_lock(tmp_path):
    assert not (tmp_path / "sicherheitssatz.json").exists()
    _store(tmp_path).empfangen("konfiguration", _konfiguration())
    gespeichert = json.loads((tmp_path / "sicherheitssatz.json").read_text())
    assert (gespeichert["hebelsatz"], gespeichert["verteilsystem"]) == ("viessmann_vicare", "Heizkoerper")


def _scheitert_fuer(monkeypatch, name: str) -> None:
    """dateien.schreiben_json scheitert fuer die Datei `name` (Stromausfall bzw. Datentraeger), alle anderen gehen."""
    echt = dateien.schreiben_json

    def schreiben(pfad, *args, **kwargs):
        if pfad.name == name:
            raise OSError("Datentraeger")
        return echt(pfad, *args, **kwargs)

    monkeypatch.setattr(docs.dateien, "schreiben_json", schreiben)


def test_an_abort_after_the_lock_leaves_the_lock_without_the_document(tmp_path, monkeypatch):
    """Spec 3.2: die Rastung steht vor dem Dokument; ein Abbruch dazwischen hinterlaesst die Rastung ohne Dokument
    (der Server stellt neu zu, die Rastung passt dazu)."""
    _scheitert_fuer(monkeypatch, "konfiguration.json")
    with pytest.raises(OSError):
        _store(tmp_path).empfangen("konfiguration", _konfiguration())
    monkeypatch.undo()
    assert rastung.lesen(tmp_path / "sicherheitssatz.json") == rastung.Rastung("viessmann_vicare", "Heizkoerper")
    assert not (tmp_path / "konfiguration.json").exists() and _store(tmp_path).version("konfiguration") == 0


def test_an_empty_configuration_never_creates_a_lock(tmp_path):
    _store(tmp_path).empfangen("konfiguration", _leer())
    assert not (tmp_path / "sicherheitssatz.json").exists()


def test_a_stored_configuration_without_lock_locks_on_start(tmp_path):
    _store(tmp_path).empfangen("konfiguration", _konfiguration())
    (tmp_path / "sicherheitssatz.json").unlink()  # Stand vor dieser Version (client1)
    store = _store(tmp_path)
    assert store.rastung == rastung.Rastung("viessmann_vicare", "Heizkoerper")
    assert (tmp_path / "sicherheitssatz.json").exists()


def test_a_broken_lock_file_rejects_every_set_up_configuration(tmp_path):
    (tmp_path / "sicherheitssatz.json").write_text("kaputt")
    store = _store(tmp_path)
    assert store.empfangen("konfiguration", _konfiguration()).grund == "hebelsatz_eingerastet"
    assert store.empfangen("konfiguration", _leer(version=2)).ok  # leer bleibt moeglich, die Datei bleibt kaputt
    assert (tmp_path / "sicherheitssatz.json").read_text() == "kaputt"


def test_a_symlinked_lock_file_is_neither_followed_nor_replaced(tmp_path):
    """Review-Fokus 1: ein Symlink sperrt; er wird nicht aus der gespeicherten Konfiguration neu gerastet."""
    _store(tmp_path).empfangen("konfiguration", _konfiguration())
    echt = tmp_path / "fremd.json"
    (tmp_path / "sicherheitssatz.json").replace(echt)
    (tmp_path / "sicherheitssatz.json").symlink_to(echt)
    store = _store(tmp_path)
    assert store.rastung == rastung.UNBEKANNT
    assert (tmp_path / "sicherheitssatz.json").is_symlink()
    assert store.empfangen("konfiguration", _konfiguration(version=2)).grund == "hebelsatz_eingerastet"
    assert (tmp_path / "sicherheitssatz.json").is_symlink()


def test_a_dangling_symlinked_lock_file_creates_no_file_at_the_target(tmp_path):
    ziel = tmp_path / "ziel.json"
    (tmp_path / "sicherheitssatz.json").symlink_to(ziel)
    store = _store(tmp_path)
    assert store.empfangen("konfiguration", _konfiguration()).grund == "hebelsatz_eingerastet"
    assert not ziel.exists()


def test_a_new_configuration_is_taken_and_survives_a_restart(tmp_path):
    store = _store(tmp_path)
    ergebnis = store.empfangen("konfiguration", _konfiguration())
    assert ergebnis == docs.Ergebnis("konfiguration", 1, True, uebernommen=True)
    assert ergebnis.payload() == {"schema": 1, "dokument": "konfiguration", "version": 1, "ok": True}
    again = _store(tmp_path)
    assert again.version("konfiguration") == 1 and again.inhalt("konfiguration")["anlage"] == "kunde3"
    assert "schema" not in again.inhalt("konfiguration") and "version" not in again.inhalt("konfiguration")
    assert again.eingerichtet()


def test_without_documents_the_versions_are_zero(tmp_path):
    store = _store(tmp_path)
    assert (store.version("konfiguration"), store.version("bedienung"), store.eingerichtet()) == (0, 0, False)


def test_a_broken_document_file_counts_as_no_version(tmp_path):
    _store(tmp_path).empfangen("konfiguration", _konfiguration())
    (tmp_path / docs.DATEIEN["konfiguration"]).write_text('{"version": 1, "inh')  # Stromausfall
    store = _store(tmp_path)
    assert store.version("konfiguration") == 0 and store.inhalt("konfiguration") is None


def test_the_same_version_again_only_repeats_the_result(tmp_path):
    store = _store(tmp_path)
    store.empfangen("konfiguration", _konfiguration())
    again = store.empfangen("konfiguration", _konfiguration(raumfuehler=["treiber:room_temperature"]))
    assert again == docs.Ergebnis("konfiguration", 1, True)
    assert store.inhalt("konfiguration")["raumfuehler"] == [SENSOR]


def test_any_other_version_is_taken_also_a_smaller_one(tmp_path):
    store = _store(tmp_path)
    store.empfangen("konfiguration", _konfiguration(version=7))
    assert store.empfangen("konfiguration", _konfiguration(version=3, tagestick="06:00")).uebernommen
    assert store.version("konfiguration") == 3


def test_a_rejection_keeps_the_previous_configuration_and_is_remembered(tmp_path):
    store = _store(tmp_path)
    store.empfangen("konfiguration", _konfiguration())
    ergebnis = store.empfangen("konfiguration", _konfiguration(version=2, verteilsystem="Fussbodenheizung"))
    assert (ergebnis.ok, ergebnis.grund, ergebnis.uebernommen) == (False, "hebelsatz_eingerastet", False)
    assert ergebnis.payload()["error"] == {"grund": "hebelsatz_eingerastet"}
    assert store.version("konfiguration") == 1
    assert _store(tmp_path).ablehnung("konfiguration") == (2, "hebelsatz_eingerastet", None)
    store.empfangen("konfiguration", _konfiguration(version=3, tagestick="06:00"))
    assert store.ablehnung("konfiguration") is None


def test_invalid_binding_parameters_are_rejected_as_invalid_and_keep_the_previous_configuration(tmp_path):
    store = _store(tmp_path)
    store.empfangen("konfiguration", _konfiguration())
    locker = parameter("viessmann_vicare", schreibschutz={"tag": None, "lebensdauer_hinweis": None,
                                                           "durchsetzen_tag": 5})
    ergebnis = store.empfangen("konfiguration", _konfiguration(version=2, bindungsparameter=locker))
    assert (ergebnis.ok, ergebnis.grund, ergebnis.uebernommen) == (False, "ungueltig", False)
    assert ergebnis.details == {"feld": "durchsetzen_tag", "regel": "bereich", "min": 1, "max": 4}
    assert store.version("konfiguration") == 1
    assert store.ablehnung("konfiguration") == (2, "ungueltig", ergebnis.details)


def test_an_unknown_schema_is_refused_and_means_update_needed(tmp_path):
    store = _store(tmp_path)
    ergebnis = store.empfangen("konfiguration", {**_konfiguration(), "schema": 2})
    assert (ergebnis.ok, ergebnis.grund) == (False, "schema_unbekannt") and store.update_noetig


def test_missing_fields_are_invalid(tmp_path):
    body = _konfiguration()
    del body["tagestick"]
    ergebnis = _store(tmp_path).empfangen("konfiguration", body)
    assert ergebnis.grund == "ungueltig"
    assert ergebnis.details == {"feld": "tagestick", "regel": "fehlt", "anzahl": 1}


def test_eine_ablehnung_von_vor_5c_wird_ohne_text_geladen(tmp_path):
    (tmp_path / docs.ZUSTAND_DATEI).write_text(json.dumps(
        {"update_anlass": None, "abgelehnt": {"konfiguration": [3, "ungueltig", "Alter Gerätetext"]}}))
    assert _store(tmp_path).ablehnung("konfiguration") == (3, "ungueltig", None)


def test_die_bedienung_nennt_das_feld(tmp_path):
    store = _store(tmp_path)
    assert store.empfangen("bedienung", _bedienung(raum_soll=25.5)).details == {
        "feld": "raum_soll", "regel": "bereich", "min": 15.0, "max": 25.0}
    assert store.empfangen("bedienung", _bedienung(version=2, modus="programm")).details == {
        "feld": "modus", "regel": "unbekannt"}


def test_ungueltige_details_des_hosts_gehen_nicht_hoch(tmp_path, caplog):
    store = docs.DocumentStore(tmp_path, host="gateway", software_version="0.6.0",
                               treiber_pruefen=lambda bindung: {"feld": "sensor.leonies_zimmer"})
    ergebnis = store.empfangen("konfiguration", _konfiguration())
    assert ergebnis.payload()["error"] == {"grund": "treiber_parameter"}
    assert "leonies" not in caplog.text


SOFTWARE = {"version": "0.7.0", "manifest_url": "https://example.invalid/m.json", "manifest_sha256": "a" * 64,
            "signature": "c2ln"}


@pytest.mark.parametrize(("fremd", "details"), [
    ({"kuenftig": {"a": 1}}, {"regel": "unbekannt"}),
    ({"intervalle": {"pruefung_s": 5}}, {"regel": "unbekannt"}),
    ({"treiber": "x"}, {"feld": "treiber", "regel": "unbekannt"}),  # steht im Vertrag, aber nicht in der Konfiguration
])
def test_a_foreign_configuration_field_is_rejected(tmp_path, fremd, details):
    """V1 ist auf beiden Seiten strikt (Nutzer-Entscheidung 2026-10-16, R10); der Name geht nur hoch, wenn er im Vertrag
    steht (wire.PRUEF_FELDER)."""
    store = _store(tmp_path)
    ergebnis = store.empfangen("konfiguration", _konfiguration(**fremd))
    assert (ergebnis.ok, ergebnis.grund, ergebnis.details) == (False, "ungueltig", details)
    assert store.inhalt("konfiguration") is None


def test_a_foreign_field_in_an_empty_configuration_is_rejected_too(tmp_path):
    assert _store(tmp_path).empfangen("konfiguration", {**_leer(), "kuenftig": 1}).grund == "ungueltig"


@pytest.mark.parametrize("software", [SOFTWARE, None])
def test_the_target_software_is_taken_with_exactly_the_contract_fields(tmp_path, software):
    assert _store(tmp_path).empfangen("konfiguration", _konfiguration(software=software)).ok


@pytest.mark.parametrize(("software", "regel"), [
    ({**SOFTWARE, "extra": 1}, "unbekannt"), ({k: v for k, v in SOFTWARE.items() if k != "signature"}, "fehlt"),
    ([], "ungueltig"), ("0.7.0", "ungueltig"), ({}, "fehlt"),
])
def test_the_target_software_must_have_exactly_the_contract_fields(tmp_path, software, regel):
    ergebnis = _store(tmp_path).empfangen("konfiguration", _konfiguration(software=software))
    assert (ergebnis.ok, ergebnis.grund) == (False, "ungueltig")
    assert ergebnis.details["feld"] == "software" and ergebnis.details["regel"] == regel


def test_a_rejected_foreign_field_changes_neither_the_accepted_document_nor_the_update_trigger(tmp_path):
    store = _store(tmp_path)
    assert store.empfangen("konfiguration", _konfiguration(version=1)).uebernommen
    vorher = store.aktuell("konfiguration")
    ergebnis = store.empfangen("konfiguration", _konfiguration(version=2, mindest_software="9.9.9", kuenftig=1))
    assert (ergebnis.ok, ergebnis.grund) == (False, "ungueltig")
    assert store.aktuell("konfiguration") == vorher and not store.update_noetig
    # ein gesetzter Ausloeser bleibt ebenfalls: die fremde Konfiguration mit niedriger Mindestversion loest ihn nicht
    abgelehnt = store.empfangen("konfiguration", _konfiguration(version=3, mindest_software="9.9.9"))
    assert abgelehnt.grund == "update_noetig"
    assert store.update_noetig
    store.empfangen("konfiguration", _konfiguration(version=4, mindest_software="0.1.0", kuenftig=1))
    assert store.update_noetig and store.aktuell("konfiguration") == vorher


def test_a_foreign_operation_field_is_rejected(tmp_path):
    store = _store(tmp_path)
    ergebnis = store.empfangen("bedienung", _bedienung(kuenftig=1))
    assert (ergebnis.ok, ergebnis.grund, ergebnis.details) == (False, "ungueltig", {"regel": "unbekannt"})
    assert store.inhalt("bedienung") is None


def test_an_operation_without_a_contract_field_is_rejected(tmp_path):
    bedienung = _bedienung()
    del bedienung["ts"]
    ergebnis = _store(tmp_path).empfangen("bedienung", bedienung)
    assert (ergebnis.ok, ergebnis.grund, ergebnis.details) == (False, "ungueltig", {"feld": "ts", "regel": "fehlt",
                                                                                     "anzahl": 1})


def test_a_reason_outside_the_contract_is_a_programming_error(tmp_path):
    with pytest.raises(ValueError, match="DOKUMENT_ERRORS"):
        _store(tmp_path)._abgelehnt_mit("konfiguration", 1, "gibt_es_nicht", None)


def test_a_configuration_with_origin_values_is_taken(tmp_path):
    store = _store(tmp_path)
    assert store.empfangen("konfiguration", _konfiguration(ursprungswerte={"heat_limit": 18.0})).ok
    assert store.inhalt("konfiguration")["ursprungswerte"] == {"heat_limit": 18.0}


@pytest.mark.parametrize("version", [0, -1, "1", True, None, 1.5])
def test_unreadable_versions_get_no_answer(tmp_path, version):
    assert _store(tmp_path).empfangen("konfiguration", {**_konfiguration(), "version": version}) is None


def test_a_configuration_below_the_minimum_software_is_refused_with_update_needed(tmp_path):
    store = _store(tmp_path, version="0.5.9")
    ergebnis = store.empfangen("konfiguration", _konfiguration())
    assert (ergebnis.ok, ergebnis.grund) == (False, "update_noetig") and store.update_noetig
    assert _store(tmp_path, version="0.5.9").update_noetig  # auch nach einem Neustart


def test_an_empty_configuration_is_taken_even_below_the_minimum(tmp_path):
    store = _store(tmp_path, version="0.5.9")
    store.empfangen("konfiguration", _konfiguration(mindest_software="0.5.0"))
    assert store.empfangen("konfiguration", _leer(2)).uebernommen
    assert not store.eingerichtet() and store.update_noetig


def test_a_configuration_within_reach_clears_update_needed(tmp_path):
    store = _store(tmp_path)
    store.empfangen("konfiguration", _konfiguration(mindest_software="0.7.0"))
    assert store.update_noetig
    assert store.empfangen("konfiguration", _konfiguration(version=2)).uebernommen and not store.update_noetig


def test_update_needed_ends_once_the_device_reaches_the_rejected_minimum(tmp_path):
    store = _store(tmp_path, version="0.5.9")
    assert store.empfangen("konfiguration", _konfiguration()).grund == "update_noetig"
    assert not _store(tmp_path, version="0.6.0").update_noetig  # aktualisiert: der Server schickt nichts erneut
    assert _store(tmp_path, version="0.5.9").update_noetig  # dieselbe alte Software: weiter noetig


def test_update_needed_after_an_unknown_schema_ends_with_a_new_software(tmp_path):
    store = _store(tmp_path, version="0.6.0")
    store.empfangen("konfiguration", {**_konfiguration(), "schema": 2})
    assert _store(tmp_path, version="0.6.0").update_noetig
    assert not _store(tmp_path, version="0.6.1").update_noetig


def test_an_invalid_configuration_keeps_update_needed(tmp_path):
    store = _store(tmp_path, version="0.5.9")
    store.empfangen("konfiguration", _konfiguration())
    body = _konfiguration(version=2)
    del body["mindest_software"]
    assert store.empfangen("konfiguration", body).grund == "ungueltig"
    assert store.update_noetig and _store(tmp_path, version="0.5.9").update_noetig


def test_the_driver_check_of_the_host_is_used(tmp_path):
    store = docs.DocumentStore(tmp_path, host="gateway", software_version="0.6.0",
                               treiber_pruefen=lambda bindung: "Heizkreis fehlt.")
    assert store.empfangen("konfiguration", _konfiguration()).grund == "treiber_parameter"


@pytest.mark.parametrize("eigene, mindest, unter", [
    ("0.6.0", "0.6.1", True), ("0.10.0", "0.9.9", False), ("0.6.0", "0.6.0", False), ("x", "0.6.0", False),
    ("0.6.0", None, False), ("1.0.0", "0.6.0", False),
])
def test_minimum_software_compares_numerically(eigene, mindest, unter):
    assert docs.unter_mindest(eigene, mindest) is unter


# --- Bedienung ---

@pytest.mark.parametrize("value, ok", [
    (15, True), (25, True), (20.5, True), (14.5, False), (25.5, False), (20.25, False), (True, False), (None, False),
    ("20", False), (math.nan, False), (math.inf, False), (10**400, False),
])
def test_operating_range(value, ok):
    assert docs.im_bedienbereich(value) is ok


def test_an_operation_is_checked_against_the_operating_range(tmp_path):
    store = _store(tmp_path)
    assert store.empfangen("bedienung", _bedienung(raum_soll=25.5)).grund == "ausserhalb_bereich"
    assert store.empfangen("bedienung", _bedienung(raum_soll=20.3)).grund == "ausserhalb_bereich"
    assert store.empfangen("bedienung", _bedienung(modus="programm")).grund == "ungueltig"
    assert store.empfangen("bedienung", _bedienung(quelle="app")).grund == "ungueltig"
    assert store.empfangen("bedienung", _bedienung(raum_soll=21.5)).uebernommen


def test_dirty_operation_takes_the_same_version_again(tmp_path):
    store = _store(tmp_path)
    store.empfangen("bedienung", _bedienung(version=3))
    store.mark_dirty()
    assert store.aktuell("bedienung").dirty and _store(tmp_path).aktuell("bedienung").dirty
    ergebnis = store.empfangen("bedienung", _bedienung(version=3, raum_soll=20.0))
    assert ergebnis.uebernommen and not store.aktuell("bedienung").dirty
    assert store.inhalt("bedienung")["raum_soll"] == 20.0


def test_dirty_without_an_operation_does_nothing(tmp_path):
    store = _store(tmp_path)
    store.mark_dirty()
    assert store.aktuell("bedienung") is None


def test_configuration_and_operation_do_not_influence_each_other(tmp_path):
    store = _store(tmp_path)
    store.empfangen("konfiguration", _konfiguration())
    assert not store.empfangen("bedienung", _bedienung(raum_soll=30.0)).ok
    assert store.version("konfiguration") == 1 and store.ablehnung("konfiguration") is None


def test_documents_are_the_contract_documents():
    assert (docs.KONFIGURATION, docs.BEDIENUNG) == wire.DOKUMENTE


@pytest.mark.parametrize("art, inhalt", [("konfiguration", _konfiguration(anlage="a\ud800b")),
                                          ("bedienung", _bedienung(ts="\udfff"))])
def test_a_document_with_a_lone_surrogate_is_rejected_and_nothing_is_stored(tmp_path, art, inhalt):
    """json.loads liefert aus "\\ud800" ein einzelnes Surrogat; schreiben_json koennte es nicht kodieren. Statt einer
    Ausnahme aus empfangen() (ohne Antwort an den Server) gibt es eine Ablehnung `ungueltig` (Testoffensive T2, Task 13)."""
    store = _store(tmp_path)
    ergebnis = store.empfangen(art, inhalt)
    assert (ergebnis.ok, ergebnis.grund, ergebnis.details) == (False, "ungueltig", None)
    assert store.inhalt(art) is None and _store(tmp_path).ablehnung(art) == (1, "ungueltig", None)
    assert store.empfangen(art, _konfiguration() if art == "konfiguration" else _bedienung()).uebernommen  # Gegenprobe


# --- Vorbereitung vor dem Speichern (Spec Testoffensive 12.6, Nr. 38: Aufraeum-Markierung) ---

def _ohne_huelle(body: dict) -> dict:
    return {key: value for key, value in body.items() if key not in ("schema", "version")}


def _vorbereitender_store(tmp_path, vorbereiten):
    return docs.DocumentStore(tmp_path, host="gateway", software_version="0.6.0", vor_speichern=vorbereiten)


def test_the_preparation_gets_old_and_new_before_the_configuration_is_written(tmp_path):
    gesehen = []

    def vorbereiten(alt, neu):
        datei = tmp_path / "konfiguration.json"
        gesehen.append((alt, neu, json.loads(datei.read_text())["version"] if datei.exists() else None))

    store = _vorbereitender_store(tmp_path, vorbereiten)
    assert store.empfangen("konfiguration", _konfiguration(version=1)).uebernommen
    assert store.empfangen("konfiguration", _leer(version=2)).uebernommen
    assert gesehen == [(None, _ohne_huelle(_konfiguration()), None),
                       (_ohne_huelle(_konfiguration()), _ohne_huelle(_leer()), 1)]  # im Rueckruf: noch die alte Datei
    assert store.version("konfiguration") == 2


@pytest.mark.parametrize("fehler", [OSError("/data/aufraeumen.json"), RuntimeError("unerwartet")])
def test_a_failing_preparation_holds_the_configuration_back_and_stores_nothing(tmp_path, fehler):
    """Nr. 38: scheitert die Vorbereitung (OSError beim Schreiben der Markierung, aber auch jede andere Ausnahme), ist
    die Konfiguration nicht pruefbar: nichts gespeichert, nichts abgelehnt, das Geraet haelt sie zurueck (device.py)."""
    def vorbereiten(alt, neu):
        if not neu.get("anlage"):
            raise fehler

    store = _vorbereitender_store(tmp_path, vorbereiten)
    store.empfangen("konfiguration", _konfiguration(version=1))
    vorher = {name: (tmp_path / name).read_bytes() for name in ("konfiguration.json", "dokumente.json")}
    with pytest.raises(config_check.NichtPruefbar):
        store.empfangen("konfiguration", _leer(version=2))
    assert {name: (tmp_path / name).read_bytes() for name in vorher} == vorher
    assert store.version("konfiguration") == 1 and store.eingerichtet() and store.ablehnung("konfiguration") is None
    assert _store(tmp_path).version("konfiguration") == 1


def test_a_failing_preparation_of_the_first_configuration_does_not_lock(tmp_path):
    def vorbereiten(alt, neu):
        raise OSError("Datentraeger")

    with pytest.raises(config_check.NichtPruefbar):
        _vorbereitender_store(tmp_path, vorbereiten).empfangen("konfiguration", _konfiguration())
    assert not (tmp_path / "sicherheitssatz.json").exists() and not (tmp_path / "konfiguration.json").exists()


def test_a_held_back_configuration_is_logged_once_until_a_preparation_succeeds(tmp_path, caplog):
    """Eine ERROR-Zeile je Zurueckhalten (das Geraet wiederholt alle 30 s), Fehlerart ohne Pfad und Text; nach einem
    Erfolg meldet ein neues Scheitern sich wieder."""
    kaputt = [True]

    def vorbereiten(alt, neu):
        if kaputt[0]:
            raise OSError("/data/aufraeumen.json geheim")

    store = _vorbereitender_store(tmp_path, vorbereiten)
    with caplog.at_level("DEBUG", logger="smartheat_device"):
        for _ in range(2):
            with pytest.raises(config_check.NichtPruefbar):
                store.empfangen("konfiguration", _konfiguration(version=1))
        assert [r.levelname for r in caplog.records] == ["ERROR"]
        kaputt[0] = False
        assert store.empfangen("konfiguration", _konfiguration(version=1)).uebernommen
        kaputt[0] = True
        with pytest.raises(config_check.NichtPruefbar):
            store.empfangen("konfiguration", _leer(version=2))
    fehler = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(fehler) == 2 and all("OSError" in r.getMessage() for r in fehler)
    assert "geheim" not in caplog.text and "aufraeumen" not in caplog.text


def test_operations_rejections_and_duplicates_skip_the_preparation(tmp_path):
    gesehen = []
    store = docs.DocumentStore(tmp_path, host="gateway", software_version="0.6.0",
                               treiber_pruefen=lambda bindung: {"feld": "parameter", "regel": "ungueltig"},
                               vor_speichern=lambda alt, neu: gesehen.append(neu))
    assert store.empfangen("bedienung", _bedienung()).uebernommen
    assert store.empfangen("konfiguration", _konfiguration(version=1)).grund == "treiber_parameter"
    assert store.empfangen("konfiguration", {**_konfiguration(version=2), "schema": 99}).grund == "schema_unbekannt"
    assert store.empfangen("konfiguration", _konfiguration(version=3, anlage=None, abo="x", kuenftig=1)).grund \
        == "ungueltig"
    assert store.empfangen("konfiguration", _leer(version=4)).uebernommen  # Gegenprobe: uebernommen -> gerufen
    assert store.empfangen("konfiguration", _leer(version=4)).ok  # Duplikat
    assert gesehen == [_ohne_huelle(_leer())]
