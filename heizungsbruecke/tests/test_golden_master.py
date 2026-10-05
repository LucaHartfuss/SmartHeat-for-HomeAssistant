"""Golden-Master (Spec SHG 3.4): der Szenario-Lauf muss bytegleich zur Aufzeichnung vom Stand vor G1 sein.
Neu schreiben nur mit GOLDEN_UPDATE=1 und nur in Task 1 des Plans SHG G1."""
import os
from datetime import date
from pathlib import Path

import pytest
from golden_scenario import render, run_scenario

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
