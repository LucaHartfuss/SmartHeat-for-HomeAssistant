"""Spec Stufe 3 S3 Abschnitt 4: leer heisst abmelden."""
import errno
import os
import stat
from pathlib import Path

import pytest

from smartheat_runtime import aufraeumen

ALT = {"anlage": "kunde1", "hebelsatz": "vaillant_vrc720", "verteilsystem": "Heizkoerper", "ursprungswerte": {}}
LEER = {"anlage": None}


def test_no_marker_means_nothing_to_do(tmp_path):
    assert aufraeumen.entscheiden(tmp_path / "aufraeumen.json", LEER) == aufraeumen.KEINE


def test_marker_keeps_the_old_configuration_privately(tmp_path):
    pfad = tmp_path / "aufraeumen.json"
    aufraeumen.markieren(pfad, ALT)
    assert aufraeumen.lesen(pfad) == aufraeumen.Markierung("kunde1", ALT, False)
    assert stat.S_IMODE(pfad.stat().st_mode) == 0o600
    assert aufraeumen.entscheiden(pfad, LEER) == aufraeumen.ZURUECKSETZEN
    assert aufraeumen.laufzeit_konfiguration(LEER, pfad) == (ALT, True)


def test_after_the_reset_the_state_is_deleted(tmp_path):
    pfad = tmp_path / "aufraeumen.json"
    aufraeumen.markieren(pfad, ALT)
    assert aufraeumen.zurueckgesetzt(pfad, "kunde1") is True
    assert aufraeumen.zurueckgesetzt(pfad, "kunde1") is False  # nur einmal
    assert aufraeumen.entscheiden(pfad, LEER) == aufraeumen.LOESCHEN
    assert aufraeumen.laufzeit_konfiguration(LEER, pfad) == (LEER, False)


def test_reset_of_another_plant_does_not_count(tmp_path):
    pfad = tmp_path / "aufraeumen.json"
    aufraeumen.markieren(pfad, ALT)
    assert aufraeumen.zurueckgesetzt(pfad, "kunde2") is False
    assert aufraeumen.entscheiden(pfad, LEER) == aufraeumen.ZURUECKSETZEN


def test_new_setup_before_reset_discards_marker(tmp_path):
    pfad = tmp_path / "aufraeumen.json"
    aufraeumen.markieren(pfad, ALT)
    assert aufraeumen.entscheiden(pfad, ALT) == aufraeumen.VERWERFEN
    assert aufraeumen.laufzeit_konfiguration(ALT, pfad) == (ALT, False)


def test_marker_without_configuration_is_deleted_as_before(tmp_path):
    pfad = tmp_path / "aufraeumen.json"
    pfad.write_text('{"anlage": "kunde1"}')  # Markierung aus 1.0.0
    assert aufraeumen.entscheiden(pfad, LEER) == aufraeumen.LOESCHEN
    pfad.write_text("{")
    assert aufraeumen.entscheiden(pfad, LEER) == aufraeumen.LOESCHEN


def test_marker_whose_configuration_names_another_plant_is_ignored(tmp_path):
    pfad = tmp_path / "aufraeumen.json"
    pfad.write_text('{"anlage": "kunde1", "konfiguration": {"anlage": "kunde9"}, "zurueckgesetzt": false}')
    assert aufraeumen.lesen(pfad).konfiguration is None
    assert aufraeumen.entscheiden(pfad, LEER) == aufraeumen.LOESCHEN


@pytest.mark.parametrize("code", [errno.EIO, errno.EACCES])
def test_an_unreadable_marker_is_kept_and_nothing_is_deleted(tmp_path, monkeypatch, caplog, code):
    """S3S3-Marker: vorhanden, aber unlesbar -> behalten und loggen, nie wie eine fehlende werten."""
    pfad = tmp_path / "aufraeumen.json"
    aufraeumen.markieren(pfad, ALT)
    echt = Path.read_bytes

    def lesen(self):
        if self == pfad:
            raise OSError(code, "geheim")
        return echt(self)

    monkeypatch.setattr(Path, "read_bytes", lesen)
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: lesen(self).decode())
    assert aufraeumen.entscheiden(pfad, LEER) == aufraeumen.BEHALTEN
    art = type(OSError(code, "")).__name__  # EACCES wird zu PermissionError, EIO bleibt OSError
    assert "aufraeumen.json" in caplog.text and art in caplog.text and "geheim" not in caplog.text


def test_a_broken_marker_is_still_deleted_as_before(tmp_path):
    pfad = tmp_path / "aufraeumen.json"
    pfad.write_text("{kaputt")
    assert aufraeumen.entscheiden(pfad, LEER) == aufraeumen.LOESCHEN


def test_a_new_setup_discards_even_an_unreadable_marker(tmp_path, monkeypatch):
    pfad = tmp_path / "aufraeumen.json"
    aufraeumen.markieren(pfad, ALT)
    monkeypatch.setattr(Path, "read_bytes", lambda self: (_ for _ in ()).throw(OSError(errno.EIO, "x")))
    assert aufraeumen.entscheiden(pfad, ALT) == aufraeumen.VERWERFEN  # Zustand bleibt ohnehin


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
    assert aufraeumen.entscheiden(pfad, LEER) == aufraeumen.BEHALTEN
    assert "aufraeumen.json" in caplog.text and "OSError" in caplog.text and "geheim" not in caplog.text


def test_a_marker_that_vanishes_between_exists_and_reading_is_decided_by_lesen(tmp_path, monkeypatch):
    """Rennen: exists() sah die Markierung, read_bytes() nicht mehr (FileNotFoundError). Das ist kein Lesefehler
    (nicht BEHALTEN), sondern eine fehlende Markierung: lesen() findet keine Konfiguration, also LOESCHEN wie bisher."""
    pfad = tmp_path / "aufraeumen.json"
    aufraeumen.markieren(pfad, ALT)

    def verschwunden(self):
        pfad.unlink()
        raise FileNotFoundError(pfad.name)

    monkeypatch.setattr(Path, "read_bytes", verschwunden)
    assert aufraeumen.entscheiden(pfad, LEER) == aufraeumen.LOESCHEN
