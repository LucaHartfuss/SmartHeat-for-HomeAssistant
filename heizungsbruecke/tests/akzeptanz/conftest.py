"""Sicherung der Akzeptanzsuite. Die Suite laeuft nur allein (pytest --confcutdir=tests/akzeptanz tests/akzeptanz): ohne
das lade pytest die Fixtures und Hilfen der uebrigen Tests mit, und die Suite waere nicht mehr unabhaengig von ihnen.
Maschinell geprueft werden: jede ID aus IDS hat genau einen Test, kein Test ohne Marker oder mit unbekannter ID, kein
skip und kein xfail (auch nicht zur Laufzeit), jeder Test laeuft und besteht (kein -k, --deselect, -m, --co: ein
unvollstaendiger Lauf endet mit Exit 1), keine Importe ausser Standardbibliothek, pytest, cryptography und dem
Produkt, und die Signaturen der Fake-HA gleichen denen von HomeAssistantApi."""
import ast
import inspect
import os
import sys
from pathlib import Path

import pytest

ORDNER = Path(__file__).resolve().parent
#: alle Szenarien dieser Suite; jede ID hat genau einen Test mit @pytest.mark.akzeptanz("<ID>")
IDS = (
    "AK-G1-01", "AK-G1-03", "AK-G1-07", "AK-G2-04", "AK-G2-05", "AK-G2-06",
    "AK-G3-01", "AK-G3-02", "AK-G3-03", "AK-G4-01", "AK-G4-02", "AK-G4-05", "AK-G5-01",
    "AK-G6-01", "AK-G6-02", "AK-G6-03", "AK-G7-01", "AK-G8-01", "AK-G8-02", "AK-G8-03", "AK-G9-01", "AK-G9-02", "AK-G9-06",
    "AK-G9-07", "AK-G9-08", "AK-G9-09", "AK-G9-10", "AK-G9-11", "AK-G10-01",
)
#: Tests der Suite (Node-ID) und die, die bestanden haben; die Deckung der Laeufe prueft pytest_sessionfinish
GESAMMELT: set[str] = set()
BESTANDEN: set[str] = set()
ERLAUBT = {"pytest", "cryptography", "heizungsbruecke"}
UEBERSPRUNGEN = ("skip", "skipif", "xfail")


def pytest_configure(config):
    config.addinivalue_line("markers", "akzeptanz(id): Szenario der Akzeptanzsuite (genau ein Test je ID)")
    cut = config.getoption("confcutdir")
    if cut is None or Path(str(cut)).resolve() != ORDNER:
        pytest.exit("Akzeptanzsuite: nur mit --confcutdir=tests/akzeptanz starten (die Hilfen der uebrigen Tests "
                    "duerfen nicht geladen werden)", returncode=1)
    fehler = _importfehler() + _signaturfehler()
    if fehler:
        pytest.exit("Akzeptanzsuite: " + "; ".join(fehler), returncode=1)


def _importfehler() -> list[str]:
    eigene = {datei.stem for datei in ORDNER.glob("*.py")}
    fehler = []
    for datei in sorted(ORDNER.glob("*.py")):
        for knoten in ast.walk(ast.parse(datei.read_text(encoding="utf-8"))):
            if isinstance(knoten, ast.Import):
                module = [alias.name for alias in knoten.names]
            elif isinstance(knoten, ast.ImportFrom):
                module = [("." * knoten.level) + (knoten.module or "")]
            else:
                continue
            for name in module:
                wurzel = name.split(".")[0]
                if not (wurzel in sys.stdlib_module_names or wurzel in ERLAUBT or wurzel in eigene
                        or wurzel.startswith("smartheat_")):
                    fehler.append(f"{datei.name}: Import {name!r} nicht erlaubt")
    return fehler


def _signaturfehler() -> list[str]:
    from welt import FakeHa

    from heizungsbruecke.ha_api import HomeAssistantApi

    def form(funktion):
        return [(p.name, p.kind, p.default) for p in inspect.signature(funktion).parameters.values()]

    fehler = []
    for name, wert in vars(FakeHa).items():
        if name.startswith("_") or not inspect.isfunction(wert):
            continue
        echt = getattr(HomeAssistantApi, name, None)
        if echt is None:
            fehler.append(f"FakeHa.{name} hat kein Gegenstueck in HomeAssistantApi")
        elif form(wert) != form(echt):
            fehler.append(f"FakeHa.{name}{inspect.signature(wert)} weicht von HomeAssistantApi ab")
    return fehler


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Deckung ueber alle gesammelten Tests, noch vor jeder Auswahl mit -k."""
    fehler, funktionen = [], {}
    for item in items:
        marker = item.get_closest_marker("akzeptanz")
        if marker is None or len(marker.args) != 1 or not isinstance(marker.args[0], str):
            fehler.append(f"{item.nodeid}: ohne Marker akzeptanz(\"AK-...\")")
            continue
        if any(item.get_closest_marker(name) for name in UEBERSPRUNGEN):
            fehler.append(f"{item.nodeid}: skip/xfail ist nicht erlaubt")
        GESAMMELT.add(item.nodeid)
        funktionen.setdefault(marker.args[0], set()).add(item.nodeid.partition("[")[0])
    for ident, namen in funktionen.items():
        if ident not in IDS:
            fehler.append(f"unbekannte ID {ident}")
        if len(namen) > 1:
            fehler.append(f"ID {ident} doppelt: {sorted(namen)}")
    fehler += [f"ID {ident} ohne Test" for ident in IDS if ident not in funktionen]
    if fehler:
        pytest.exit("Akzeptanzsuite: " + "; ".join(fehler), returncode=1)


def pytest_runtest_logreport(report):
    if report.when == "call" and report.passed:
        BESTANDEN.add(report.nodeid)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session):
    """Gruen heisst: jeder Test der Suite lief und bestand. Abgewaehlte, nicht gelaufene oder nicht bestandene Tests
    (-k, --deselect, -m, --co, -x, PYTEST_ADDOPTS, addopts) machen den Lauf rot."""
    fehlend = sorted(GESAMMELT - BESTANDEN)
    if fehlend and session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        bericht = session.config.pluginmanager.get_plugin("terminalreporter")
        text = f"Akzeptanzsuite unvollstaendig: {len(fehlend)} von {len(GESAMMELT)} Tests nicht bestanden, z. B. {fehlend[0]}"
        if bericht is not None:
            bericht.write_line(text, red=True)
        else:
            print(text)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Ein uebersprungener oder als erwartet fehlschlagend gewerteter Test (auch zur Laufzeit) ist rot."""
    ergebnis = yield
    bericht = ergebnis.get_result()
    if bericht.skipped or hasattr(bericht, "wasxfail"):
        bericht.outcome = "failed"
        bericht.longrepr = "Akzeptanzsuite: skip und xfail sind nicht erlaubt"
        if hasattr(bericht, "wasxfail"):
            del bericht.wasxfail


@pytest.fixture(autouse=True)
def _ohne_fsync(monkeypatch):
    """Durchsatz: jedes atomare Schreiben ruft fsync; die Absturzsicherheit pruefen die Tests der Dateien."""
    monkeypatch.setattr(os, "fsync", lambda fd: None)


@pytest.fixture
def welt(tmp_path, monkeypatch):
    """Fabrik: baut die Anlage fuer (Hebelsatz, Verteilsystem) in tmp_path; alles Weitere regelt die Welt."""
    from welt import Anlage

    def bauen(satz: str, verteilsystem: str) -> Anlage:
        return Anlage(tmp_path, monkeypatch, satz, verteilsystem)

    return bauen
