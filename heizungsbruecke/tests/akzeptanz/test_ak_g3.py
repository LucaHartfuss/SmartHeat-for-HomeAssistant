"""Fussbodenheizung: lokale Obergrenzen (Estrich), kein Comfort-Boost, Notfall-Boost nur bis zum lokalen Maximum.
Die Grenzen stehen hier woertlich, nie aus dem Produktcode."""
import pytest
from anlagen import ALLE, hebel_von, schreibziel
from welt import Anlage

#: lokales Maximum je (Hebelsatz, Verteilsystem) und Hebel (Regel 4), woertlich
MAXIMA = {
    ("vaillant_vrc720", "Heizkoerper"): {"curve": 1.5, "room_setpoint": 25.0, "heat_limit": 23.0},
    ("weishaupt_wwp", "Heizkoerper"): {"curve": 1.00, "room_setpoint": 25.0, "heat_limit": 23.0},
    ("weishaupt_wwp_basis", "Heizkoerper"): {"room_setpoint": 25.0},
    ("viessmann_vicare", "Heizkoerper"): {"curve": 1.4, "level": 3.0, "room_setpoint": 25.0},
    ("vaillant_vrc720", "Fussbodenheizung"): {"curve": 0.35, "room_setpoint": 24.0, "heat_limit": 20.0},
    ("weishaupt_wwp", "Fussbodenheizung"): {"curve": 0.50, "room_setpoint": 24.0, "heat_limit": 20.0},
    ("weishaupt_wwp_basis", "Fussbodenheizung"): {"room_setpoint": 24.0},
    ("viessmann_vicare", "Fussbodenheizung"): {"curve": 0.4, "level": 3.0, "room_setpoint": 24.0},
}
#: Werte mitten im Bereich der Fussbodenheizung: ein Boost wuerde sie auf das Maximum heben
MITTE = {
    "vaillant_vrc720": {"curve": 0.2, "room_setpoint": 20.0, "heat_limit": 10.0},
    "weishaupt_wwp": {"curve": 0.3, "room_setpoint": 20.0, "heat_limit": 10.0},
    "weishaupt_wwp_basis": {"room_setpoint": 20.0},
    "viessmann_vicare": {"curve": 0.3, "level": 0.0, "room_setpoint": 20.0},
}
#: was ein kaputter oder boesartiger Server fordert: weit ueber jedem Maximum
FORDERUNG = {"curve": 1.5, "room_setpoint": 30.0, "heat_limit": 23.0, "level": 10.0}
FBH = [(satz, v) for satz, v in ALLE if v == "Fussbodenheizung"]


def _ist(anlage: Anlage, satz: str, hebel_namen) -> dict:
    """Letzter geschriebener Wert je Hebel."""
    ist = {entity: wert for entity, wert, _ in anlage.ha.schreibvorgaenge}
    return {hebel: ist.get(schreibziel(satz, hebel)) for hebel in hebel_namen}


def _hoechste(anlage: Anlage, satz: str, hebel: str) -> float:
    werte = [w for e, w, _ in anlage.ha.schreibvorgaenge if e == schreibziel(satz, hebel)]
    assert werte, hebel
    return max(werte)


@pytest.mark.akzeptanz("AK-G3-01")
@pytest.mark.parametrize("satz,verteilsystem", ALLE, ids=[f"{s}-{v}" for s, v in ALLE])
def test_ak_g3_01_obergrenzen_und_kein_comfort_boost(welt, satz, verteilsystem):
    maxima = MAXIMA[(satz, verteilsystem)]
    assert set(maxima) == set(hebel_von(satz))
    anlage: Anlage = welt(satz, verteilsystem)
    anlage.starten()  # Soll 21,0
    if verteilsystem == "Fussbodenheizung":
        # Werte mitten im Bereich, kalter Raum, der Bewohner hebt den Wunsch: bei Heizkoerpern gaebe das den Boost
        mitte = MITTE[satz]
        anlage.server_antwortet(mitte)
        anlage.runde(2)
        assert _ist(anlage, satz, mitte) == mitte
        anlage.raum(19.0)
        anlage.runde(2)
        geschrieben = len(anlage.ha.schreibvorgaenge)
        anlage.wunsch(22.0)
        anlage.runde(2)
        neu = [(e, w) for e, w, _ in anlage.ha.schreibvorgaenge[geschrieben:]]
        assert all(w == mitte[h] for e, w in neu for h in mitte if e == schreibziel(satz, h)), neu
        assert _ist(anlage, satz, mitte) == mitte
    anlage.server_antwortet({hebel: FORDERUNG[hebel] for hebel in maxima})  # weit ueber jedem Maximum
    anlage.runde(2)
    assert _ist(anlage, satz, maxima) == maxima
    for hebel, maximum in maxima.items():  # nie ein Schreibvorgang darueber, auch nicht auf dem Weg dorthin
        assert _hoechste(anlage, satz, hebel) == maximum, hebel


@pytest.mark.akzeptanz("AK-G3-02")
@pytest.mark.parametrize("satz,verteilsystem", FBH, ids=[f"{s}-{v}" for s, v in FBH])
def test_ak_g3_02_notfall_boost_nur_bis_zum_maximum(welt, satz, verteilsystem):
    maxima = MAXIMA[(satz, verteilsystem)]
    anlage: Anlage = welt(satz, verteilsystem)
    anlage.starten()  # Soll 21,0; der Server schweigt
    anlage.laufen(75)  # Notbetrieb nach zwei Fehlschlaegen
    geschrieben = len(anlage.ha.schreibvorgaenge)
    anlage.raum(19.0)  # 2 K unter Soll
    anlage.runde(2)
    assert len(anlage.ha.schreibvorgaenge) > geschrieben  # der Notfall-Boost schreibt
    assert _ist(anlage, satz, maxima) == maxima
    for hebel, maximum in maxima.items():
        assert _hoechste(anlage, satz, hebel) <= maximum, hebel
