"""Golden-Master: der Szenario-Lauf muss bytegleich zur Aufzeichnung sein.
Neu schreiben nur mit GOLDEN_UPDATE=1 und nur, wenn sich die Aufzeichnung gewollt aendert (neue backup.json-Felder, eine
andere Reihenfolge in der Telemetrie, die Kennung des Heizkreises u. a.); der Master des Geraete-Pfads (1.0.0) ist der
aktuelle, der Master von 0.36 bleibt als addon_scenario_0_36.json, der Unterschied ist auf golden_scenario.ERLAUBT
begrenzt."""
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


def test_status_start_is_only_normalised_in_the_two_allowed_steps(tmp_path, monkeypatch):
    """ERLAUBT "status_start" beschreibt genau den Unterschied in abo_erneut_inaktiv und abmelden: zeigt ein anderer
    Schritt "startet" bei Notbetrieb (hier: der Schritt notbetrieb statt "notbetrieb"), faellt das als Unterschied zu
    0.36 auf."""
    neu = run_scenario(tmp_path, monkeypatch)
    alt = json.loads(GOLDEN_0_36.read_text(encoding="utf-8"))
    assert normalisieren(neu, alt=False) == normalisieren(alt, alt=True)
    manipuliert = json.loads(json.dumps(neu))
    geaendert = 0
    for schritt in manipuliert:
        if schritt["schritt"] == "notbetrieb":
            for w in schritt["wirkungen"]:
                if w[:3] == ["ha", "event", "smartheat_status"] and w[3]["status"] == "notbetrieb":
                    w[3]["status"] = "startet"
                    geaendert += 1
    assert geaendert > 0 and manipuliert != neu
    assert normalisieren(manipuliert, alt=False) != normalisieren(alt, alt=True)
