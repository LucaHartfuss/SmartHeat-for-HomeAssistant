"""Spec 5b 3 und 5.1: Dokumentspeicher des Geraets und atomares Schreiben (Plan D1 Task 3)."""
import json
import math

import pytest
from bindung_beispiele import parameter

from smartheat_core import rastung
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


def test_the_first_set_up_configuration_locks_before_it_is_stored(tmp_path, monkeypatch):
    store = _store(tmp_path)
    reihenfolge = []
    echt_rasten, echt_speichern = store._rasten, store._speichern
    monkeypatch.setattr(store, "_rasten", lambda inhalt: reihenfolge.append("rasten") or echt_rasten(inhalt))
    monkeypatch.setattr(store, "_speichern", lambda doc: reihenfolge.append("speichern") or echt_speichern(doc))
    store.empfangen("konfiguration", _konfiguration())
    assert reihenfolge == ["rasten", "speichern"]
    assert json.loads((tmp_path / "sicherheitssatz.json").read_text())["verteilsystem"] == "Heizkoerper"


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
    assert ergebnis.payload()["error"] == {"grund": "hebelsatz_eingerastet", "text": ergebnis.text} and ergebnis.text
    assert store.version("konfiguration") == 1
    assert _store(tmp_path).ablehnung("konfiguration") == (2, "hebelsatz_eingerastet", ergebnis.text)
    store.empfangen("konfiguration", _konfiguration(version=3, tagestick="06:00"))
    assert store.ablehnung("konfiguration") is None


def test_invalid_binding_parameters_are_rejected_as_invalid_and_keep_the_previous_configuration(tmp_path):
    store = _store(tmp_path)
    store.empfangen("konfiguration", _konfiguration())
    locker = parameter("viessmann_vicare", schreibschutz={"tag": None, "lebensdauer_hinweis": None,
                                                           "durchsetzen_tag": 5})
    ergebnis = store.empfangen("konfiguration", _konfiguration(version=2, bindungsparameter=locker))
    assert (ergebnis.ok, ergebnis.grund, ergebnis.uebernommen) == (False, "ungueltig", False)
    assert ergebnis.text == "Durchsetzen je Tag muss in den Bindungsparametern zwischen 1 und 4 liegen."
    assert store.version("konfiguration") == 1
    assert store.ablehnung("konfiguration") == (2, "ungueltig", ergebnis.text)


def test_an_unknown_schema_is_refused_and_means_update_needed(tmp_path):
    store = _store(tmp_path)
    ergebnis = store.empfangen("konfiguration", {**_konfiguration(), "schema": 2})
    assert (ergebnis.ok, ergebnis.grund) == (False, "schema_unbekannt") and store.update_noetig


def test_missing_fields_are_invalid(tmp_path):
    body = _konfiguration()
    del body["tagestick"]
    ergebnis = _store(tmp_path).empfangen("konfiguration", body)
    assert ergebnis.grund == "ungueltig" and "tagestick" in ergebnis.text


def test_unknown_fields_are_kept_and_ignored(tmp_path):
    store = _store(tmp_path)
    assert store.empfangen("konfiguration", _konfiguration(kuenftig={"a": 1})).ok
    assert store.inhalt("konfiguration")["kuenftig"] == {"a": 1}


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
    assert "0.5.9" in ergebnis.text and "0.6.0" in ergebnis.text
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
