"""Gemeinsame Beobachtung der Boost-Szenarien: Werte des Comfort-Boosts der Vaillant-Heizkoerper (woertlich, nie aus
dem Produktcode) und was die Anlage zeigt."""
from anlagen import schreibziel

HEBEL = ("curve", "room_setpoint", "heat_limit")
COMFORT_BOOST = {"curve": 1.5, "room_setpoint": 25.0, "heat_limit": 23.0}
REGELWERTE = {"curve": 0.9, "room_setpoint": 22.0, "heat_limit": 16.0}  # was der Server fuer Soll 22 fordert
SATZ = "vaillant_vrc720"


def stand(anlage, satz: str = SATZ) -> dict:
    """Aktueller Wert der drei Hebel an der Anlage."""
    return {hebel: anlage.ha.states[schreibziel(satz, hebel)] for hebel in HEBEL}


def boost_starts(anlage, satz: str = SATZ) -> list[float]:
    """Zeitpunkte (Fake-Uhr), zu denen die Kurve auf den Boost-Wert gesetzt wurde."""
    ziel = schreibziel(satz, "curve")
    return [zeit for entity, wert, zeit in anlage.ha.schreibvorgaenge if entity == ziel and wert == COMFORT_BOOST["curve"]]


def boost_anlage(welt, satz: str = SATZ):
    """Warmer Start (21,0 bei Soll 21,0), Server antwortet, dann der Bewohner hebt den Wunsch bei kaltem Raum auf 22:
    Der Comfort-Boost laeuft."""
    from anlagen import RAUMFUEHLER

    anlage = welt(satz, "Heizkoerper")
    anlage.ha.states[RAUMFUEHLER] = 21.0
    anlage.starten()
    anlage.server_antwortet({"curve": 0.8, "room_setpoint": 21.0, "heat_limit": 15.0})
    anlage.runde(2)
    anlage.raum(19.0)
    anlage.wunsch(22.0)
    anlage.runde(2)
    anlage.server_antwortet(REGELWERTE)
    anlage.runde(2)
    assert stand(anlage) == COMFORT_BOOST
    return anlage
