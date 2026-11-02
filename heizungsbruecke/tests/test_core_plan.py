import ast
import dataclasses
import pathlib
from datetime import UTC, datetime

import pytest

from smartheat_core import plan, wallclock
from smartheat_core.plan import SollErgebnis, soll_effektiv

JETZT = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
ZONE = "Europe/Berlin"
KONFIGURATION: dict = {}


def bedienung(raum_soll=21.0):
    return {"kreis": 1, "modus": "manuell", "raum_soll": raum_soll}


def test_ohne_plan_ist_das_soll_der_gemerkte_wunsch():
    ergebnis = soll_effektiv(JETZT, ZONE, bedienung(21.5), KONFIGURATION)
    assert ergebnis == SollErgebnis(soll=21.5, soll_art="wunsch", naechster_wechsel=None)


def test_ergebnis_ist_unveraenderlich():
    ergebnis = soll_effektiv(JETZT, ZONE, bedienung(), KONFIGURATION)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ergebnis.soll = 5.0  # type: ignore[misc]


@pytest.mark.parametrize("wunsch", [5.0, 35.0, 5, 35, 4.9, 35.1])
def test_wunsch_am_rand_wird_unveraendert_durchgereicht(wunsch):
    # Die Grenzen des Raumbereichs setzt der Wunsch-Eingang; hier wird nichts begrenzt.
    assert soll_effektiv(JETZT, ZONE, bedienung(wunsch), KONFIGURATION).soll == wunsch


def test_soll_quelle_aus_oder_frostschutz_ergibt_kein_soll():
    ergebnis = soll_effektiv(JETZT, ZONE, bedienung(21.5), KONFIGURATION, soll_quelle_aus=True)
    assert ergebnis == SollErgebnis(soll=None, soll_art=None, naechster_wechsel=None)


def test_soll_quelle_an_ist_der_standard():
    assert soll_effektiv(JETZT, ZONE, bedienung(20.0), KONFIGURATION, soll_quelle_aus=False).soll == 20.0


def test_naive_jetzt_wird_abgelehnt():
    with pytest.raises(ValueError, match="^jetzt braucht eine Zeitzone$"):
        soll_effektiv(datetime(2026, 11, 2, 12, 0), ZONE, bedienung(), KONFIGURATION)


@pytest.mark.parametrize("jetzt", ["2026-11-02T12:00:00+00:00", None, 1.0])
def test_jetzt_ohne_datum_wird_abgelehnt(jetzt):
    with pytest.raises(ValueError, match="^jetzt braucht eine Zeitzone$"):
        soll_effektiv(jetzt, ZONE, bedienung(), KONFIGURATION)


@pytest.mark.parametrize("zone", ["Mars/Olympus", "", "../etc/passwd", "Europe/Berlin\x00", None, 3, "/etc"])
def test_ungueltige_zone_wird_abgelehnt_ohne_den_namen_zu_nennen(zone):
    with pytest.raises(ValueError, match="^zone ist keine bekannte Zeitzone$") as fehler:
        soll_effektiv(JETZT, zone, bedienung(), KONFIGURATION)
    assert "Mars" not in str(fehler.value)


@pytest.mark.parametrize("wunsch", [float("nan"), float("inf"), True, None, "21", [21.0]])
def test_unbrauchbarer_wunsch_wird_abgelehnt_ohne_den_wert_zu_nennen(wunsch):
    with pytest.raises(ValueError, match="^bedienung hat keinen brauchbaren Wunsch$"):
        soll_effektiv(JETZT, ZONE, bedienung(wunsch), KONFIGURATION)


@pytest.mark.parametrize("doc", [None, [], "x", {"kreis": 1}])
def test_bedienung_ohne_wunsch_wird_abgelehnt(doc):
    with pytest.raises(ValueError, match="^bedienung hat keinen brauchbaren Wunsch$"):
        soll_effektiv(JETZT, ZONE, doc, KONFIGURATION)


@pytest.mark.parametrize("konfiguration", [None, [], "x"])
def test_konfiguration_ohne_objekt_wird_abgelehnt(konfiguration):
    with pytest.raises(ValueError, match="^konfiguration ist kein Objekt$"):
        soll_effektiv(JETZT, ZONE, bedienung(), konfiguration)


def test_die_rechnung_ist_rein_und_wiederholbar(monkeypatch):
    # Keine Uhr: die Wanduhr des Clients wird nicht gelesen, das Ergebnis haengt nur von den Eingaben ab.
    def verboten():
        raise AssertionError("Uhr gelesen")

    monkeypatch.setattr(wallclock, "_now", verboten)
    eingabe = bedienung(19.0)
    erstes = soll_effektiv(JETZT, ZONE, eingabe, KONFIGURATION)
    assert soll_effektiv(JETZT, ZONE, eingabe, KONFIGURATION) == erstes
    assert eingabe == bedienung(19.0)


def test_die_zeitzone_des_hosts_aendert_nichts(monkeypatch):
    import time

    erstes = soll_effektiv(JETZT, ZONE, bedienung(), KONFIGURATION)
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    time.tzset()
    try:
        assert soll_effektiv(JETZT, ZONE, bedienung(), KONFIGURATION) == erstes
    finally:
        monkeypatch.undo()
        time.tzset()


def test_modul_importiert_nur_reine_bausteine_und_oeffnet_keine_dateien():
    baum = ast.parse(pathlib.Path(plan.__file__).read_text(encoding="utf-8"))
    importiert = set()
    for knoten in ast.walk(baum):
        if isinstance(knoten, ast.Import):
            importiert |= {a.name.split(".")[0] for a in knoten.names}
        elif isinstance(knoten, ast.ImportFrom) and knoten.module:
            importiert.add(knoten.module.split(".")[0])
    assert importiert <= {"__future__", "dataclasses", "datetime", "collections", "zoneinfo", "smartheat_core"}
    aufgerufen = {k.func.id for k in ast.walk(baum) if isinstance(k, ast.Call) and isinstance(k.func, ast.Name)}
    assert not aufgerufen & {"open", "print", "exec", "eval", "__import__"}
    assert "wallclock" not in importiert and "time" not in importiert
