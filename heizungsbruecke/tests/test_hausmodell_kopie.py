"""Kopie des Hausmodell-Kerns: ein von Hand gerechneter Vektor laeuft am Import, und kein Modul ausser dem
Simulationstreiber importiert sie (ein Kunde fuehrt den Simulationsweg nie aus). Die Zeichengleichheit mit der Quelle
im Server prueft der Contract-Check des Kern-Repos."""
import ast
from pathlib import Path

import pytest

from smartheat_core import hausmodell
from smartheat_core.hausmodell import Anlagenzustand, Betriebsflags, House, Potenzgesetz, anlage_schritt

REPO = Path(__file__).resolve().parents[2]
ERLAUBTER_IMPORTEUR = REPO / "gateway" / "src" / "smartheat_gateway" / "drivers" / "simulation.py"
ZIEL = 20.5


def test_heizender_schritt_stimmt_mit_der_handrechnung():
    # Hebel (1,0; 20,0; 15,0), Aussen 0, Raum 20,5, Heizbetrieb an, Mittag, keine Sonne, ohne Zufallsfolge.
    erzeuger = Potenzgesetz(2.55, 0.78)
    zustand = Anlagenzustand(room=ZIEL, heating=True)
    schritt = anlage_schritt(zustand, House(), erzeuger, (1.0, 20.0, 15.0), 0.0, 0.0, 12.0, ZIEL, Betriebsflags(), None)
    vorlauf = 2.55 * 20.0**0.78 + 20.0
    assert schritt.flow == pytest.approx(vorlauf, abs=1e-12) and schritt.flow > ZIEL
    assert schritt.geliefert and schritt.brenner and schritt.gemessen is None
    leistung = 38.8 * (vorlauf - ZIEL) ** 1.3  # erste Naeherung; der Fixpunkt senkt sie um die Spreizung
    assert 0.0 < schritt.leistung < leistung
    raum = ZIEL + (schritt.leistung - 150.0 * (ZIEL - 0.0) + 300.0) * 300.0 / (150.0 * 40.0 * 3600)
    assert zustand.room == pytest.approx(raum, abs=1e-12)
    assert zustand.burner_starts == 20001 and zustand.burner_was_on


def test_ohne_heizbetrieb_kuehlt_der_raum_nach_der_bilanz():
    zustand = Anlagenzustand(room=ZIEL)
    schritt = anlage_schritt(zustand, House(), Potenzgesetz(2.55, 0.78), (1.0, 10.0, 15.0), 13.8, 0.0, 12.0, ZIEL,
                             Betriebsflags(), None)
    assert not zustand.heating and schritt.flow == 0.0 and not schritt.geliefert
    assert zustand.room == pytest.approx(ZIEL + (-150.0 * 6.7 + 300.0) * 300.0 / 21.6e6, abs=1e-12)


def _importiert_hausmodell(pfad: Path) -> bool:
    for knoten in ast.walk(ast.parse(pfad.read_text(encoding="utf-8"))):
        if isinstance(knoten, ast.Import) and any(a.name.endswith("hausmodell") for a in knoten.names):
            return True
        if isinstance(knoten, ast.ImportFrom) and (
            (knoten.module or "").endswith("hausmodell") or any(a.name == "hausmodell" for a in knoten.names)
        ):
            return True
    return False


def test_nur_der_simulationstreiber_importiert_den_kern():
    quellen = sorted((REPO / "heizungsbruecke" / "src").rglob("*.py")) + sorted((REPO / "gateway" / "src").rglob("*.py"))
    kopie = Path(hausmodell.__file__).resolve()
    assert kopie in [p.resolve() for p in quellen], "die Suche sieht die Kopie nicht: der Test waere leer"
    fremde = [p for p in quellen if p.resolve() != kopie and p != ERLAUBTER_IMPORTEUR and _importiert_hausmodell(p)]
    assert fremde == []


def test_die_suche_erkennt_einen_import(tmp_path):
    for text in ("import smartheat_core.hausmodell", "from smartheat_core import hausmodell",
                 "from smartheat_core.hausmodell import House", "from . import hausmodell as h"):
        datei = tmp_path / "x.py"
        datei.write_text(text + "\n", encoding="utf-8")
        assert _importiert_hausmodell(datei), text
    datei.write_text("import math\n", encoding="utf-8")
    assert not _importiert_hausmodell(datei)
