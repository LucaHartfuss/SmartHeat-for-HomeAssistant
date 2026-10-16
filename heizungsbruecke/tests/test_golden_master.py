"""Golden-Master (Spec SHG 3.4): der Szenario-Lauf muss bytegleich zur Aufzeichnung vom Stand vor G1 sein.
Neu schreiben nur mit GOLDEN_UPDATE=1 und nur in Task 1 des Plans SHG G1
und in Task 1 des Plans Audit 4 P-C2 (neues backup.json-Feld boost_since)
und in Task 3 des Plans Audit 4 P-C2 (neue backup.json-Felder setup_id, plant_id)
und in Task 9 des Plans Audit 4 P-B (Telemetrie ohne waerme_fehlt, Status ohne Hinweis waerme_fehlt, Version 0.36.0)
und in Task 10 des Plans 5c D (Geraete-Pfad, 1.0.0; der Master von 0.36 bleibt als addon_scenario_0_36.json, der
Unterschied ist auf golden_scenario.ERLAUBT begrenzt)
und in Task 10 des Plans Stufe 3 S3 (Version normalisiert, A5-65)
und in Task 3 des Plans Stufe 3 S7 (setup_id entfaellt, A5-63: per Skript entfernt bzw. in backup.json zu tenant_id, nicht neu erzeugt)
und in Task 2 des Plans Stufe 3 S8a-1 (raum_soll_wirksam entfaellt aus dem Snapshot, A5-43: per Skript entfernt, nicht neu
erzeugt)."""
import json
import os
import time
from datetime import date
from pathlib import Path

import pytest
from golden_scenario import normalisieren, render_ohne_version, run_scenario


@pytest.fixture(autouse=True)
def _restore_process_timezone():
    """Das Szenario setzt TZ per monkeypatch und ruft tzset(); nach dem Zuruecksetzen der Umgebung nachziehen."""
    yield
    time.tzset()


GOLDEN = Path(__file__).resolve().parent / "golden" / "addon_scenario.json"
GOLDEN_0_36 = GOLDEN.with_name("addon_scenario_0_36.json")


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


def test_only_the_allowed_differences_to_0_36(tmp_path, monkeypatch):
    neu = run_scenario(tmp_path, monkeypatch)
    alt = json.loads(GOLDEN_0_36.read_text(encoding="utf-8"))
    assert normalisieren(neu, alt=False) == normalisieren(alt, alt=True)
