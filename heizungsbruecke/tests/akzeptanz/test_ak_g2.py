"""Kaelte-Szenarien: der Comfort-Boost kommt nur bei einer Erhoehung des Wunsches, nie aus Kaelte allein.
Die Boost-Werte stehen hier woertlich, nie aus dem Produktcode."""
import pytest
from anlagen import RAUMFUEHLER, schreibziel
from welt import Anlage

HEBEL = ("curve", "room_setpoint", "heat_limit")
COMFORT_BOOST = {"curve": 1.5, "room_setpoint": 25.0, "heat_limit": 23.0}  # Vaillant x Heizkoerper, woertlich
REGELWERTE = {"curve": 0.9, "room_setpoint": 22.0, "heat_limit": 16.0}  # was der Server fuer Soll 22 fordert


def _stand(anlage: Anlage, satz: str) -> dict:
    """Aktueller Wert der drei Hebel an der Anlage."""
    return {hebel: anlage.ha.states[schreibziel(satz, hebel)] for hebel in HEBEL}


def _schreibungen(anlage: Anlage, satz: str) -> int:
    """Zahl der Schreibvorgaenge auf die drei Hebel (der abgeleitete Mindestvorlauf bleibt aussen vor)."""
    ziele = {schreibziel(satz, hebel) for hebel in HEBEL}
    return len([1 for entity, _, _ in anlage.ha.schreibvorgaenge if entity in ziele])


@pytest.mark.akzeptanz("AK-G2-04")
def test_ak_g2_04_comfort_boost_nur_bei_erhoehung(welt):
    satz = "vaillant_vrc720"
    anlage: Anlage = welt(satz, "Heizkoerper")
    anlage.ha.states[RAUMFUEHLER] = 21.0  # warm: schon beim Start soll kein Boost aus Kaelte entstehen
    anlage.starten()  # Soll 21,0
    anlage.server_antwortet({"curve": 0.8, "room_setpoint": 21.0, "heat_limit": 15.0})
    anlage.runde(2)
    ruhig = _stand(anlage, satz)
    geschrieben = _schreibungen(anlage, satz)
    anlage.raum(19.0)  # 2 K zu kalt, aber keine Erhoehung: kein Boost
    anlage.runde(2)
    assert _stand(anlage, satz) == ruhig and _schreibungen(anlage, satz) == geschrieben
    anlage.wunsch(20.0)  # Absenkung bei kaltem Raum: ebenfalls kein Boost
    anlage.runde(2)
    assert _stand(anlage, satz) == ruhig and _schreibungen(anlage, satz) == geschrieben
    anlage.server_antwortet({"curve": 0.8, "room_setpoint": 20.0, "heat_limit": 15.0})
    anlage.runde(2)
    anlage.wunsch(22.0)  # der Bewohner hebt den Wunsch 20 -> 22, der Raum ist bei 19
    anlage.runde(2)
    assert _stand(anlage, satz) == COMFORT_BOOST
    anlage.server_antwortet(REGELWERTE)  # der Server antwortet, der Boost bleibt bis zur Ankunft
    anlage.runde(2)
    assert _stand(anlage, satz) == COMFORT_BOOST
    anlage.raum(21.4)  # noch unter Soll - 0,5
    anlage.runde(2)
    assert _stand(anlage, satz) == COMFORT_BOOST
    anlage.raum(21.5)  # Soll - 0,5 erreicht: Rueckkehr auf die Regelwerte
    anlage.runde(2)
    assert _stand(anlage, satz) == REGELWERTE
