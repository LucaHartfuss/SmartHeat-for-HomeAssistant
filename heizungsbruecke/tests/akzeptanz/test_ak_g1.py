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


HEBEL = ("curve", "room_setpoint", "heat_limit")
NOTFALL_BOOST = {"curve": 1.5, "room_setpoint": 25.0, "heat_limit": 23.0}  # Vaillant x Heizkoerper, woertlich


def _stand(anlage, satz):
    """Aktueller Wert der drei Hebel an der Anlage."""
    return {hebel: anlage.ha.states[schreibziel(satz, hebel)] for hebel in HEBEL}


@pytest.mark.akzeptanz("AK-G1-03")
def test_ak_g1_03_notbetrieb_und_notfall_boost(welt):
    satz = "vaillant_vrc720"
    anlage: Anlage = welt(satz, "Heizkoerper")
    anlage.konfiguration["bindung"]["benachrichtigung"] = ["notify.mobile_app_handy"]
    anlage.starten()  # Soll 21,0; der Server schweigt (Ack verweigert)
    anlage.laufen(40)  # ein Fehlschlag: der Tick wird neu gesendet, noch kein Notbetrieb
    assert len(anlage.link.of("up/snapshot")) == 2
    assert not any("Notbetrieb aktiv" in text for text in anlage.meldetexte())
    anlage.laufen(35)  # zweiter Fehlschlag: Notbetrieb mit Meldung in HA
    assert any("Notbetrieb aktiv" in text for text in anlage.meldetexte())
    vor, geschrieben = _stand(anlage, satz), len(anlage.ha.schreibvorgaenge)
    anlage.raum(20.0)  # genau 1,0 K unter Soll: kein Notfall-Boost
    anlage.runde(2)
    assert len(anlage.ha.schreibvorgaenge) == geschrieben and _stand(anlage, satz) == vor
    anlage.raum(19.9)  # 1,1 K unter Soll
    anlage.runde(2)
    assert _stand(anlage, satz) == NOTFALL_BOOST
    anlage.raum(20.4)  # noch unter Soll - 0,5: der Boost bleibt
    anlage.runde(2)
    assert _stand(anlage, satz) == NOTFALL_BOOST
    anlage.raum(20.5)  # Soll - 0,5 erreicht: zurueck auf die Werte davor
    anlage.runde(2)
    assert _stand(anlage, satz) == vor
    anlage.laufen(1900)  # der naechste Notfall-Boost-Start ist erst nach einer halben Stunde erlaubt
    anlage.raum(19.9)
    anlage.runde(2)
    assert _stand(anlage, satz) == NOTFALL_BOOST
    anlage.server_antwortet({"curve": 0.8, "room_setpoint": 21.0, "heat_limit": 15.0})  # das Ack beendet den Notbetrieb
    anlage.runde(2)
    assert _stand(anlage, satz) == {"curve": 0.8, "room_setpoint": 21.0, "heat_limit": 15.0}
    assert any("Notbetrieb beendet" in text for text in anlage.meldetexte())
    assert "smartheat_notbetrieb" in anlage.zurueckgenommen()
    anlage.laufen(1900)
    geschrieben = len(anlage.ha.schreibvorgaenge)
    anlage.raum(19.0)  # ohne Notbetrieb kein Notfall-Boost mehr
    anlage.runde(2)
    assert len(anlage.ha.schreibvorgaenge) == geschrieben
