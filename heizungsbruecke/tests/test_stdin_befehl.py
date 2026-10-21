"""L4-06 Variante C (Spec 2.2): der lokale stdin-Befehl der Integration. Genau eine Zeile mit der eigenen Geraete-ID
ruft den Rueckruf; jede andere Zeile endet in genau einer WARNING ohne Zeileninhalt. Zeilen stehen woertlich im Test,
nicht aus den Konstanten des Moduls."""
import io
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from heizungsbruecke import stdin_befehl

EIGEN = "shg-aaaaaaaaaaaaaaaa"
FREMD = "shg-bbbbbbbbbbbbbbbb"
LOGGER = "heizungsbruecke.stdin_befehl"
SRC = Path(__file__).resolve().parents[1] / "src"


def _lesen(caplog, daten: bytes) -> tuple[int, list[logging.LogRecord]]:
    """Ruft lesen auf daten; liefert Zahl der Rueckrufe und die WARNING-Records (INFO und darunter ausgeschlossen)."""
    gerufen = []
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        stdin_befehl.lesen(io.BytesIO(daten), EIGEN, lambda: gerufen.append(1))
    return len(gerufen), [r for r in caplog.records if r.name == LOGGER and r.levelno >= logging.WARNING]


def test_die_eigene_zeile_ruft_entfernen_genau_einmal(caplog):
    gerufen, warnungen = _lesen(caplog, b"entfernen shg-aaaaaaaaaaaaaaaa\n")
    assert gerufen == 1
    assert warnungen == []


def test_die_eigene_zeile_ohne_zeilenende_vor_eof_gilt(caplog):
    gerufen, warnungen = _lesen(caplog, b"entfernen shg-aaaaaaaaaaaaaaaa")
    assert gerufen == 1
    assert warnungen == []


@pytest.mark.parametrize("zeile, grund", [
    (b"entfernen shg-bbbbbbbbbbbbbbbb\n", "andere_geraete_id"),
    (b"loeschen shg-aaaaaaaaaaaaaaaa\n", "unbekannt"),
    (b"ENTFERNEN shg-aaaaaaaaaaaaaaaa\n", "unbekannt"),
    (b"entfernen\n", "unbekannt"),
    (b"entfernen  shg-aaaaaaaaaaaaaaaa\n", "unbekannt"),
    (b"entfernen shg-aaaaaaaaaaaaaaaa x\n", "unbekannt"),
    (b"entfernen shg-aaaaaaaaaaaaaaaa\r\n", "andere_geraete_id"),  # CR gehoert zur ID: nicht die eigene
    (b" \n", "unbekannt"),  # nur die exakt leere Zeile ist still, Leerzeichen nicht
    (b"\xff\xfeentfernen shg-aaaaaaaaaaaaaaaa\n", "kein_utf8"),
    (b"x" * 201 + b"\n", "zu_lang"),
    (b"x" * 200 + b"\n", "unbekannt"),  # Grenze: 200 Bytes sind noch eine Zeile
])
def test_jede_andere_zeile_verwirft_mit_einer_warnung_ohne_inhalt(caplog, zeile, grund):
    gerufen, warnungen = _lesen(caplog, zeile)
    assert gerufen == 0
    assert len(warnungen) == 1
    assert grund in warnungen[0].getMessage()
    inhalt = zeile.decode("utf-8", "replace").strip()
    assert not inhalt or inhalt not in warnungen[0].getMessage()


@pytest.mark.parametrize("zeile, grund", [
    (b"geheimXYZ shg-bbbbbbbbbbbbbbbb\n", "unbekannt"),
    (b"entfernen geheimXYZ\n", "andere_geraete_id"),
    (b"\xff geheimXYZ\n", "kein_utf8"),
    (b"geheimXYZ" + b"x" * 300 + b"\n", "zu_lang"),
])
def test_der_zeileninhalt_steht_nie_im_log(caplog, zeile, grund):
    gerufen, warnungen = _lesen(caplog, zeile)
    assert gerufen == 0
    assert len(warnungen) == 1
    assert grund in warnungen[0].getMessage()
    assert "geheimXYZ" not in warnungen[0].getMessage()
    assert "geheimXYZ" not in caplog.text


def test_nach_einer_zu_langen_zeile_gilt_die_naechste(caplog):
    daten = b"x" * 500 + b"\n" + b"entfernen shg-aaaaaaaaaaaaaaaa\n"
    gerufen, warnungen = _lesen(caplog, daten)
    assert gerufen == 1
    assert len(warnungen) == 1
    assert "zu_lang" in warnungen[0].getMessage()


def test_der_supervisor_haengt_ein_zeilenende_an_und_die_leerzeile_danach_ist_still(caplog):
    """Supervisor write_stdin schreibt data + b"\\n" (supervisor/docker/app.py): auf der Leitung steht
    `entfernen <id>\\n\\n`; die Leerzeile danach ist kein Befehl und keine WARNING wert."""
    gerufen, warnungen = _lesen(caplog, b"entfernen shg-aaaaaaaaaaaaaaaa\n\n")
    assert gerufen == 1
    assert warnungen == []


def test_eine_leerzeile_allein_ist_still(caplog):
    gerufen, warnungen = _lesen(caplog, b"\n\n\n")
    assert gerufen == 0
    assert warnungen == []


def test_eine_zu_lange_zeile_bis_eof_verwirft_den_rest_mit_einer_warnung(caplog):
    gerufen, warnungen = _lesen(caplog, b"x" * 700)
    assert gerufen == 0
    assert len(warnungen) == 1


def test_eof_beendet_den_thread_still(caplog):
    gerufen = []
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        thread = stdin_befehl.starten(io.BytesIO(b""), EIGEN, lambda: gerufen.append(1))
        thread.join(timeout=2)
    assert not thread.is_alive()
    assert thread.daemon and thread.name == "stdin"
    assert gerufen == []
    assert [r for r in caplog.records if r.name == LOGGER and r.levelno >= logging.WARNING] == []


def test_der_thread_ruft_den_rueckruf_fuer_die_eigene_zeile():
    gerufen = threading.Event()
    thread = stdin_befehl.starten(io.BytesIO(b"entfernen shg-aaaaaaaaaaaaaaaa\n"), EIGEN, gerufen.set)
    thread.join(timeout=2)
    assert gerufen.is_set()


class _Kaputt:
    def __init__(self, fehler: Exception):
        self._fehler = fehler

    def readline(self, grenze=-1):
        raise self._fehler


@pytest.mark.parametrize("fehler", [OSError(5, "geheim"), ValueError("geheim: I/O operation on closed file")])
def test_ein_lesefehler_beendet_den_thread_mit_einer_warnung(caplog, fehler):
    gerufen = []
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        thread = stdin_befehl.starten(_Kaputt(fehler), EIGEN, lambda: gerufen.append(1))
        thread.join(timeout=2)
    assert not thread.is_alive()
    assert gerufen == []
    warnungen = [r for r in caplog.records if r.name == LOGGER]
    assert len(warnungen) == 1
    assert type(fehler).__name__ in warnungen[0].getMessage()
    assert "geheim" not in caplog.text


def test_ein_blockierter_stdin_thread_verhindert_kein_sauberes_ende():
    """Mit sys.stdin.buffer als Quelle bricht der Interpreter beim Ende (SIGTERM -> SystemExit) mit 'Fatal Python
    error: _enter_buffered_busy' ab (Exit 134), solange der Daemon-Thread auf einer offenen Pipe in readline blockiert.
    Die Quelle aus __main__.stdin_quelle (ungepuffert) darf das nicht. stdin bleibt offen (kein communicate)."""
    skript = (
        "import time\n"
        "from heizungsbruecke import __main__ as m, stdin_befehl\n"
        f"stdin_befehl.starten(m.stdin_quelle(), {EIGEN!r}, lambda: None)\n"
        "time.sleep(0.2)\n"
        "raise SystemExit(0)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    proc = subprocess.Popen([sys.executable, "-c", skript], stdin=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        rueckgabe = proc.wait(timeout=10)
        stderr = proc.stderr.read()
    finally:
        proc.kill()
        proc.wait()
        proc.stdin.close()
        proc.stderr.close()
    assert b"Fatal Python error" not in stderr
    assert rueckgabe == 0
