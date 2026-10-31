"""Grenz-Szenarien: was immer der Server fordert, die Anlage bekommt nie einen Wert unter dem lokalen Minimum
(und nie einen ueber dem lokalen Maximum). Die Grenzen stehen hier woertlich, nie aus dem Produktcode."""
import pytest
from anlagen import ALLE, hebel_von, schreibziel
from welt import Anlage

#: lokales Minimum je (Hebelsatz, Verteilsystem) und Hebel (Regel 4), woertlich
MINIMA = {
    ("vaillant_vrc720", "Heizkoerper"): {"curve": 0.4, "room_setpoint": 15.0, "heat_limit": 5.0},
    ("weishaupt_wwp", "Heizkoerper"): {"curve": 0.30, "room_setpoint": 16.0, "heat_limit": 5.0},
    ("weishaupt_wwp_basis", "Heizkoerper"): {"room_setpoint": 16.0},
    ("viessmann_vicare", "Heizkoerper"): {"curve": 0.4, "level": -13.0, "room_setpoint": 15.0},
    ("vaillant_vrc720", "Fussbodenheizung"): {"curve": 0.10, "room_setpoint": 15.0, "heat_limit": 5.0},
    ("weishaupt_wwp", "Fussbodenheizung"): {"curve": 0.10, "room_setpoint": 16.0, "heat_limit": 5.0},
    ("weishaupt_wwp_basis", "Fussbodenheizung"): {"room_setpoint": 16.0},
    ("viessmann_vicare", "Fussbodenheizung"): {"curve": 0.2, "level": -5.0, "room_setpoint": 15.0},
}
#: was ein kaputter oder boesartiger Server fordert: weit unter jedem Minimum
FORDERUNG = {"curve": 0.0, "room_setpoint": 5.0, "heat_limit": -10.0, "level": -50.0}


@pytest.mark.akzeptanz("AK-G1-01")
@pytest.mark.parametrize("satz,verteilsystem", ALLE, ids=[f"{s}-{v}" for s, v in ALLE])
def test_ak_g1_01_forderung_unter_dem_minimum(welt, satz, verteilsystem):
    minima = MINIMA[(satz, verteilsystem)]
    assert set(minima) == set(hebel_von(satz))
    anlage: Anlage = welt(satz, verteilsystem)
    anlage.starten()
    anlage.server_antwortet({hebel: FORDERUNG[hebel] for hebel in minima})
    anlage.runde(2)
    ist = {entity: wert for entity, wert, _ in anlage.ha.schreibvorgaenge}  # letzter Wert je Entity
    assert {hebel: ist.get(schreibziel(satz, hebel)) for hebel in minima} == minima
    for hebel, minimum in minima.items():  # nie ein Schreibvorgang darunter, auch nicht auf dem Weg dorthin
        werte = [w for e, w, _ in anlage.ha.schreibvorgaenge if e == schreibziel(satz, hebel)]
        assert werte and min(werte) == minimum, (hebel, werte)
