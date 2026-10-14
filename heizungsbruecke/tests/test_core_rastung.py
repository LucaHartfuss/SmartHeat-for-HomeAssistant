"""Spec Stufe 3 S3 Abschnitt 3: Rastung des Sicherheitssatzes; eine kaputte Datei sperrt."""
import json
import stat

import pytest

from smartheat_core import rastung

KONF = {"anlage": "a", "hebelsatz": "vaillant_vrc720", "verteilsystem": "Heizkoerper"}


def test_missing_file_means_no_lock(tmp_path):
    assert rastung.lesen(tmp_path / rastung.DATEI) is None
    assert rastung.passt(None, KONF)


def test_written_lock_is_read_back_privately(tmp_path):
    pfad = tmp_path / rastung.DATEI
    rastung.schreiben(pfad, rastung.Rastung("vaillant_vrc720", "Heizkoerper"), "2026-10-14T12:00:00+02:00")
    assert rastung.lesen(pfad) == rastung.Rastung("vaillant_vrc720", "Heizkoerper")
    assert json.loads(pfad.read_text())["seit"] == "2026-10-14T12:00:00+02:00"
    assert stat.S_IMODE(pfad.stat().st_mode) == 0o600


@pytest.mark.parametrize("inhalt", [
    "", "{", "[]", '{"hebelsatz": "vaillant_vrc720"}', '{"hebelsatz": 1, "verteilsystem": "Heizkoerper"}',
    '{"hebelsatz": "vaillant_vrc720", "verteilsystem": "Kachelofen"}', '{"hebelsatz": "", "verteilsystem": ""}',
    "[" * 50000, "x" * 100000,
])
def test_a_broken_lock_blocks_every_configuration(tmp_path, inhalt):
    pfad = tmp_path / rastung.DATEI
    pfad.write_text(inhalt)
    gelesen = rastung.lesen(pfad)
    assert gelesen == rastung.UNBEKANNT
    assert not rastung.passt(gelesen, KONF)
    assert not rastung.passt(gelesen, {**KONF, "verteilsystem": "Fussbodenheizung"})


def test_an_unreadable_lock_blocks(tmp_path):
    pfad = tmp_path / rastung.DATEI
    pfad.mkdir()  # IsADirectoryError ist ein OSError
    assert rastung.lesen(pfad) == rastung.UNBEKANNT


def test_a_symlink_counts_as_manipulated_even_to_a_valid_lock(tmp_path):
    echt = tmp_path / "echt.json"
    rastung.schreiben(echt, rastung.Rastung("vaillant_vrc720", "Heizkoerper"), "2026-10-14T12:00:00+02:00")
    pfad = tmp_path / rastung.DATEI
    pfad.symlink_to(echt)
    assert rastung.lesen(pfad) == rastung.UNBEKANNT


def test_a_dangling_symlink_blocks_instead_of_meaning_no_lock(tmp_path):
    pfad = tmp_path / rastung.DATEI
    pfad.symlink_to(tmp_path / "gibt-es-nicht.json")
    assert rastung.lesen(pfad) == rastung.UNBEKANNT


@pytest.mark.parametrize(("hebelsatz", "verteilsystem", "passt"), [
    ("vaillant_vrc720", "Heizkoerper", True), ("vaillant_vrc720", "Fussbodenheizung", False),
    ("weishaupt_wwp", "Heizkoerper", False), (None, None, False),
])
def test_only_the_same_pair_fits(hebelsatz, verteilsystem, passt):
    lock = rastung.Rastung("vaillant_vrc720", "Heizkoerper")
    assert rastung.passt(lock, {**KONF, "hebelsatz": hebelsatz, "verteilsystem": verteilsystem}) is passt


def _datei_mit_groesse(pfad, groesse):
    """Gueltiges JSON mit gueltigem Satz und genau `groesse` Bytes (die schliessende Klammer ist das letzte Byte)."""
    kopf = '{"hebelsatz": "vaillant_vrc720", "verteilsystem": "Heizkoerper", "fuell": "'
    ende = '"}'
    pfad.write_text(kopf + "x" * (groesse - len(kopf) - len(ende)) + ende)
    assert pfad.stat().st_size == groesse


@pytest.mark.parametrize(("groesse", "gesperrt"), [
    (4095, False), (4096, False), (4097, False),  # bis MAX+1 Bytes wird vollstaendig gelesen
    (4098, True),                                 # das 4098. Byte wird nicht mehr gelesen: abgeschnitten, gesperrt
])
def test_the_lock_file_is_read_up_to_a_hard_size_limit(tmp_path, groesse, gesperrt):
    """Mutationsprobe (Mutanten 0, 3, 4): Grenze des Lesepuffers (4096 + 1 Bytes)."""
    pfad = tmp_path / rastung.DATEI
    _datei_mit_groesse(pfad, groesse)
    erwartet = rastung.UNBEKANNT if gesperrt else rastung.Rastung("vaillant_vrc720", "Heizkoerper")
    assert rastung.lesen(pfad) == erwartet
