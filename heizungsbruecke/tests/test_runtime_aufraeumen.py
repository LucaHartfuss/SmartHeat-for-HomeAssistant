"""Leer heisst abmelden."""
import errno
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from smartheat_device.geraet_host_basis import GeraetHostBasis
from smartheat_runtime import aufraeumen

ALT = {"anlage": "kunde1", "hebelsatz": "vaillant_vrc720", "verteilsystem": "Heizkoerper", "ursprungswerte": {}}
LEER = {"anlage": None}


def _laufzeit(gespeichert, markierung):
    """Was der Host der Laufzeit beim Start gibt: gespeicherte Konfiguration und die beim Start gelesene Markierung."""
    host = GeraetHostBasis()
    host.verbinden(SimpleNamespace(konfiguration=lambda: gespeichert))
    host.start_markierung = markierung
    return host.laufzeit_konfiguration()


def test_no_marker_means_nothing_to_do(tmp_path):
    assert aufraeumen.entscheiden(tmp_path / "aufraeumen.json", LEER)[0] == aufraeumen.KEINE


def test_marker_keeps_the_old_configuration_privately(tmp_path):
    pfad = tmp_path / "aufraeumen.json"
    aufraeumen.markieren(pfad, ALT)
    assert aufraeumen.lesen(pfad) == aufraeumen.Markierung("kunde1", ALT, False)
    assert stat.S_IMODE(pfad.stat().st_mode) == 0o600
    entscheidung, gelesen = aufraeumen.entscheiden(pfad, LEER)
    assert entscheidung == aufraeumen.ZURUECKSETZEN and gelesen == aufraeumen.Markierung("kunde1", ALT, False)
    assert _laufzeit(LEER, gelesen) == (ALT, True)


def test_after_the_reset_the_state_is_deleted(tmp_path):
    pfad = tmp_path / "aufraeumen.json"
    aufraeumen.markieren(pfad, ALT)
    assert aufraeumen.zurueckgesetzt(pfad, "kunde1") is True
    assert aufraeumen.zurueckgesetzt(pfad, "kunde1") is False  # nur einmal
    assert aufraeumen.entscheiden(pfad, LEER) == (aufraeumen.LOESCHEN, None)
    assert _laufzeit(LEER, None) == (LEER, False)


def test_reset_of_another_plant_does_not_count(tmp_path):
    pfad = tmp_path / "aufraeumen.json"
    aufraeumen.markieren(pfad, ALT)
    assert aufraeumen.zurueckgesetzt(pfad, "kunde2") is False
    assert aufraeumen.entscheiden(pfad, LEER)[0] == aufraeumen.ZURUECKSETZEN


def test_new_setup_before_reset_discards_marker(tmp_path):
    pfad = tmp_path / "aufraeumen.json"
    aufraeumen.markieren(pfad, ALT)
    assert aufraeumen.entscheiden(pfad, ALT) == (aufraeumen.VERWERFEN, None)
    assert _laufzeit(ALT, None) == (ALT, False)


def test_a_new_setup_of_another_plant_discards_the_open_marker_and_keeps_the_state(tmp_path):
    """Auch eine andere Anlage als die der Markierung beendet das Abmelden; nichts wird geloescht."""
    pfad = tmp_path / "aufraeumen.json"
    aufraeumen.markieren(pfad, ALT)
    andere = {**ALT, "anlage": "kunde2"}
    assert aufraeumen.entscheiden(pfad, andere) == (aufraeumen.VERWERFEN, None)
    assert _laufzeit(andere, None) == (andere, False)
    assert aufraeumen.lesen(pfad) == aufraeumen.Markierung("kunde1", ALT, False)  # die Markierung wird nicht angefasst


def test_marker_without_configuration_is_deleted_as_before(tmp_path):
    pfad = tmp_path / "aufraeumen.json"
    pfad.write_text('{"anlage": "kunde1"}')  # Markierung aus 1.0.0
    assert aufraeumen.entscheiden(pfad, LEER)[0] == aufraeumen.LOESCHEN
    pfad.write_text("{")
    assert aufraeumen.entscheiden(pfad, LEER)[0] == aufraeumen.LOESCHEN


def test_marker_whose_configuration_names_another_plant_is_ignored(tmp_path):
    pfad = tmp_path / "aufraeumen.json"
    pfad.write_text('{"anlage": "kunde1", "konfiguration": {"anlage": "kunde9"}, "zurueckgesetzt": false}')
    assert aufraeumen.lesen(pfad).konfiguration is None
    assert aufraeumen.entscheiden(pfad, LEER)[0] == aufraeumen.LOESCHEN


@pytest.mark.parametrize("code", [errno.EIO, errno.EACCES])
def test_an_unreadable_marker_is_kept_and_nothing_is_deleted(tmp_path, monkeypatch, caplog, code):
    """Vorhanden, aber unlesbar -> behalten und loggen, nie wie eine fehlende werten."""
    pfad = tmp_path / "aufraeumen.json"
    aufraeumen.markieren(pfad, ALT)
    echt = Path.read_bytes

    def lesen(self):
        if self == pfad:
            raise OSError(code, "geheim")
        return echt(self)

    monkeypatch.setattr(Path, "read_bytes", lesen)
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: lesen(self).decode())
    assert aufraeumen.entscheiden(pfad, LEER)[0] == aufraeumen.BEHALTEN
    art = type(OSError(code, "")).__name__  # EACCES wird zu PermissionError, EIO bleibt OSError
    assert "aufraeumen.json" in caplog.text and art in caplog.text and "geheim" not in caplog.text


def test_a_broken_marker_is_still_deleted_as_before(tmp_path):
    pfad = tmp_path / "aufraeumen.json"
    pfad.write_text("{kaputt")
    assert aufraeumen.entscheiden(pfad, LEER)[0] == aufraeumen.LOESCHEN


@pytest.mark.parametrize("roh", [
    [{"anlage": "kunde1"}], "kunde1", {"konfiguration": {"anlage": "kunde1"}},
    {"anlage": "", "konfiguration": {"anlage": ""}}, {"anlage": 5, "konfiguration": {"anlage": 5}},
    {"anlage": None, "konfiguration": {"anlage": None}},
], ids=["liste", "text", "ohne_anlage", "anlage_leer", "anlage_zahl", "anlage_none"])
def test_a_marker_of_a_foreign_form_counts_as_broken(tmp_path, roh):
    """Gueltiges JSON in fremder Form (kein Objekt, `anlage` fehlt, leer oder kein Text) ist keine Markierung: lesen
    gibt None, entscheiden loescht wie bei kaputtem JSON (auch wenn die Konfiguration zur `anlage` passt)."""
    pfad = tmp_path / "aufraeumen.json"
    pfad.write_text(json.dumps(roh), encoding="utf-8")
    assert aufraeumen.lesen(pfad) is None
    assert aufraeumen.entscheiden(pfad, LEER)[0] == aufraeumen.LOESCHEN


def test_a_new_setup_discards_even_an_unreadable_marker(tmp_path, monkeypatch):
    pfad = tmp_path / "aufraeumen.json"
    aufraeumen.markieren(pfad, ALT)
    monkeypatch.setattr(Path, "read_bytes", lambda self: (_ for _ in ()).throw(OSError(errno.EIO, "x")))
    assert aufraeumen.entscheiden(pfad, ALT)[0] == aufraeumen.VERWERFEN  # Zustand bleibt ohnehin


def test_a_marker_whose_existence_cannot_be_determined_is_kept(tmp_path, monkeypatch, caplog):
    """Path.exists wirft bei EIO/EACCES im stat (nicht nur bei fehlender Datei): auch das darf den Start nicht kippen
    und nichts loeschen."""
    pfad = tmp_path / "aufraeumen.json"
    aufraeumen.markieren(pfad, ALT)
    echt = os.stat

    def stat_(p, *a, **k):
        if Path(p) == pfad:
            raise OSError(errno.EIO, "geheim")
        return echt(p, *a, **k)

    monkeypatch.setattr(os, "stat", stat_)
    assert aufraeumen.entscheiden(pfad, LEER)[0] == aufraeumen.BEHALTEN
    assert "aufraeumen.json" in caplog.text and "OSError" in caplog.text and "geheim" not in caplog.text


def test_a_marker_that_vanishes_between_exists_and_reading_counts_as_missing(tmp_path, monkeypatch):
    """Rennen: exists() sah die Markierung, das Lesen nicht mehr (FileNotFoundError). Das ist kein Lesefehler (nicht
    BEHALTEN) und auch keine kaputte Markierung (nicht LOESCHEN), sondern keine: KEINE."""
    pfad = tmp_path / "aufraeumen.json"
    aufraeumen.markieren(pfad, ALT)

    def verschwunden(self):
        pfad.unlink()
        raise FileNotFoundError(pfad.name)

    monkeypatch.setattr(Path, "read_bytes", verschwunden)
    assert aufraeumen.entscheiden(pfad, LEER)[0] == aufraeumen.KEINE


def _lesezaehler(monkeypatch, pfad, *, text_fehler=None):
    """Zaehlt die Lesezugriffe auf pfad ueber beide Wege (read_bytes; read_text ist der Weg von dateien.lesen_json);
    text_fehler: read_text wirft ihn fuer pfad (vorubergehender EIO genau beim zweiten Lesen)."""
    gelesen = []
    echt_bytes, echt_text = Path.read_bytes, Path.read_text

    def read_bytes(self):
        if self == pfad:
            gelesen.append("bytes")
        return echt_bytes(self)

    def read_text(self, *args, **kwargs):
        if self == pfad:
            gelesen.append("text")
            if text_fehler is not None:
                raise text_fehler
        return echt_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(Path, "read_text", read_text)
    return gelesen


def test_entscheiden_reads_the_marker_exactly_once(tmp_path, monkeypatch):
    """Ein vorubergehender EIO beim zweiten Lesen ergab LOESCHEN (die Markierung galt als kaputt), obwohl
    das erste Lesen sie fand. entscheiden liest einmal und wertet die gelesenen Bytes aus."""
    pfad = tmp_path / "aufraeumen.json"
    aufraeumen.markieren(pfad, ALT)
    gelesen = _lesezaehler(monkeypatch, pfad, text_fehler=OSError(errno.EIO, "geheim"))
    assert aufraeumen.entscheiden(pfad, LEER)[0] == aufraeumen.ZURUECKSETZEN
    assert len(gelesen) == 1


@pytest.mark.parametrize("inhalt, erwartet", [
    (None, aufraeumen.ZURUECKSETZEN), ('{"anlage": "kunde1"}', aufraeumen.LOESCHEN), ("{kaputt", aufraeumen.LOESCHEN),
], ids=["offen", "ohne_konfiguration", "kaputt"])
def test_every_decision_that_reads_reads_once(tmp_path, monkeypatch, inhalt, erwartet):
    pfad = tmp_path / "aufraeumen.json"
    aufraeumen.markieren(pfad, ALT)
    if inhalt is not None:
        pfad.write_text(inhalt)
    gelesen = _lesezaehler(monkeypatch, pfad)
    assert aufraeumen.entscheiden(pfad, LEER)[0] == erwartet
    assert len(gelesen) == 1


def test_an_unreadable_marker_is_read_once_and_kept(tmp_path, monkeypatch):
    pfad = tmp_path / "aufraeumen.json"
    aufraeumen.markieren(pfad, ALT)
    gelesen = []

    def read_bytes(self):
        gelesen.append("bytes")
        raise OSError(errno.EIO, "geheim")

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: gelesen.append("text") or "{}")
    assert aufraeumen.entscheiden(pfad, LEER)[0] == aufraeumen.BEHALTEN
    assert gelesen == ["bytes"]


@pytest.mark.parametrize("kodierung", ["utf-8-sig", "utf-16"], ids=["bom", "utf16"])
def test_entscheiden_and_lesen_decode_the_marker_alike(tmp_path, kodierung):
    """entscheiden und lesen (zurueckgesetzt, __main__) dekodieren gleich: eine offene Markierung mit UTF-8-BOM oder in
    UTF-16 ist fuer beide kaputt. Sonst ergaebe entscheiden ZURUECKSETZEN, zurueckgesetzt fande aber keine Markierung
    und erledigte sie nie."""
    pfad = tmp_path / "aufraeumen.json"
    aufraeumen.markieren(pfad, ALT)
    pfad.write_bytes(pfad.read_text(encoding="utf-8").encode(kodierung))
    assert aufraeumen.lesen(pfad) is None
    assert aufraeumen.entscheiden(pfad, LEER) == (aufraeumen.LOESCHEN, None)


def test_eio_beim_zweiten_lesen_setzt_trotzdem_zurueck(tmp_path, monkeypatch):
    """Die Markierung wird beim Start einmal gelesen und weitergereicht. Scheitert ein
    weiteres Lesen der Datei (EIO), startet die Laufzeit trotzdem im Modus abgemeldet mit der Konfiguration der
    Markierung (vorher lief sie dann ohne Zuruecksetzen bis zum naechsten Start)."""
    pfad = tmp_path / "aufraeumen.json"
    aufraeumen.markieren(pfad, ALT)
    entscheidung, gelesen = aufraeumen.entscheiden(pfad, LEER)
    assert entscheidung == aufraeumen.ZURUECKSETZEN

    def kaputt(self, *args, **kwargs):
        raise OSError(errno.EIO, "geheim")

    monkeypatch.setattr(Path, "read_bytes", kaputt)
    monkeypatch.setattr(Path, "read_text", kaputt)
    assert aufraeumen.lesen(pfad) is None  # ein zweites Lesen fande jetzt nichts mehr
    assert _laufzeit(LEER, gelesen) == (ALT, True)


def test_nur_zuruecksetzen_gibt_die_markierung_weiter(tmp_path):
    """Die Markierung kommt nur bei ZURUECKSETZEN mit: jede andere Entscheidung hat nichts zurueckzusetzen."""
    pfad = tmp_path / "aufraeumen.json"
    assert aufraeumen.entscheiden(pfad, LEER) == (aufraeumen.KEINE, None)
    aufraeumen.markieren(pfad, ALT)
    assert aufraeumen.entscheiden(pfad, ALT) == (aufraeumen.VERWERFEN, None)
    aufraeumen.zurueckgesetzt(pfad, "kunde1")
    assert aufraeumen.entscheiden(pfad, LEER) == (aufraeumen.LOESCHEN, None)
