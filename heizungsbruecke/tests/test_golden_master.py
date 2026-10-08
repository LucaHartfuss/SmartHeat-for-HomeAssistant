"""Golden-Master (Spec SHG 3.4): der Szenario-Lauf muss bytegleich zur Aufzeichnung vom Stand vor G1 sein.
Neu schreiben nur mit GOLDEN_UPDATE=1 und nur in Task 1 des Plans SHG G1
und in Task 1 des Plans Audit 4 P-C2 (neues backup.json-Feld boost_since)
und in Task 3 des Plans Audit 4 P-C2 (neue backup.json-Felder setup_id, plant_id)
und in Task 9 des Plans Audit 4 P-B (Telemetrie ohne waerme_fehlt, Status ohne Hinweis waerme_fehlt, Version 0.36.0)."""
import os
import time
from datetime import date
from pathlib import Path

import pytest
from golden_scenario import render, run_scenario


@pytest.fixture(autouse=True)
def _restore_process_timezone():
    """Das Szenario setzt TZ per monkeypatch und ruft tzset(); nach dem Zuruecksetzen der Umgebung nachziehen."""
    yield
    time.tzset()


GOLDEN = Path(__file__).resolve().parent / "golden" / "addon_scenario.json"


def test_scenario_matches_the_golden_master_byte_for_byte(tmp_path, monkeypatch):
    text = render(run_scenario(tmp_path, monkeypatch))
    if os.environ.get("GOLDEN_UPDATE") == "1":
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(text, encoding="utf-8")
        pytest.skip("Golden-Master neu geschrieben")
    actual = tmp_path / "addon_scenario.actual.json"
    actual.write_text(text, encoding="utf-8")
    assert text == GOLDEN.read_text(encoding="utf-8"), f"Abweichung, aktueller Lauf: {actual}"


def test_scenario_is_deterministic_and_never_reads_the_real_clock(tmp_path, monkeypatch):
    first = render(run_scenario(tmp_path / "a", monkeypatch))
    second = render(run_scenario(tmp_path / "b", monkeypatch))
    assert first == second
    assert date.today().isoformat() not in first  # echte Systemzeit darf nirgends durchschlagen


@pytest.mark.parametrize("outer_zone", ["UTC", "America/New_York", "Asia/Tokyo"])
def test_scenario_does_not_depend_on_the_system_timezone(tmp_path, monkeypatch, outer_zone):
    monkeypatch.setenv("TZ", outer_zone)
    time.tzset()
    assert render(run_scenario(tmp_path, monkeypatch)) == GOLDEN.read_text(encoding="utf-8")
