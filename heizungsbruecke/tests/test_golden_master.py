"""Golden-Master: der Szenario-Lauf muss bytegleich zur Aufzeichnung sein.
Neu schreiben nur mit GOLDEN_UPDATE=1 und nur, wenn sich die Aufzeichnung gewollt aendert (neue backup.json-Felder, eine
andere Reihenfolge in der Telemetrie, die Kennung des Heizkreises u. a.); der Master gilt fuer den Geraete-Pfad (1.0.0)."""
import os
import time
from datetime import date
from pathlib import Path

import pytest
from golden_scenario import render_ohne_version, run_scenario


@pytest.fixture(autouse=True)
def _restore_process_timezone():
    """Das Szenario setzt TZ per monkeypatch und ruft tzset(); nach dem Zuruecksetzen der Umgebung nachziehen."""
    yield
    time.tzset()


GOLDEN = Path(__file__).resolve().parent / "golden" / "addon_scenario.json"


def test_scenario_matches_the_golden_master_byte_for_byte(tmp_path, monkeypatch):
    text = render_ohne_version(run_scenario(tmp_path, monkeypatch))
    if os.environ.get("GOLDEN_UPDATE") == "1":
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(text, encoding="utf-8")
        pytest.skip("Golden-Master neu geschrieben")
    actual = tmp_path / "addon_scenario.actual.json"
    actual.write_text(text, encoding="utf-8")
    assert text == GOLDEN.read_text(encoding="utf-8"), f"Abweichung, aktueller Lauf: {actual}"


def test_scenario_is_deterministic_and_never_reads_the_real_clock(tmp_path, monkeypatch):
    first = render_ohne_version(run_scenario(tmp_path / "a", monkeypatch))
    second = render_ohne_version(run_scenario(tmp_path / "b", monkeypatch))
    assert first == second
    assert date.today().isoformat() not in first  # echte Systemzeit darf nirgends durchschlagen


@pytest.mark.parametrize("outer_zone", ["UTC", "America/New_York", "Asia/Tokyo"])
def test_scenario_does_not_depend_on_the_system_timezone(tmp_path, monkeypatch, outer_zone):
    monkeypatch.setenv("TZ", outer_zone)
    time.tzset()
    assert render_ohne_version(run_scenario(tmp_path, monkeypatch)) == GOLDEN.read_text(encoding="utf-8")
