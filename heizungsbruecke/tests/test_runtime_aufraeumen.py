"""Spec Stufe 3 S3 Abschnitt 4: leer heisst abmelden."""
import stat

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
