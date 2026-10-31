"""Takten: der Comfort-Boost startet hoechstens sechsmal am Tag, mit mindestens 30 min Abstand. Die Grenzen stehen
hier woertlich, nie aus dem Produktcode."""
import pytest
from anlagen import RAUMFUEHLER
from beobachtung import SATZ, boost_starts

MAX_STARTS_AM_TAG = 6
MIN_ABSTAND = 1800
ABSTAND_HEBEN = 20 * 60  # der Bewohner hebt den Wunsch alle 20 min
VERSUCHE = 14  # bei 20 min Abstand und 30 min Mindestabstand startet nur jeder zweite Versuch: 12 Versuche bis zum Limit
REGEL = {"curve": 0.8, "room_setpoint": 21.0, "heat_limit": 15.0}


@pytest.mark.akzeptanz("AK-G5-01")
def test_ak_g5_01_hoechstens_sechs_boost_starts_am_tag(welt):
    anlage = welt(SATZ, "Heizkoerper")
    anlage.ha.states[RAUMFUEHLER] = 21.0
    anlage.starten()
    anlage.server_antwortet(REGEL)
    anlage.runde(2)
    for _ in range(VERSUCHE):
        beginn = anlage.uhr()
        anlage.raum(19.0)  # kalt
        anlage.wunsch(22.0)  # der Bewohner hebt den Wunsch
        anlage.runde(2)
        anlage.vergehen(60, REGEL, fuehler=19.0)
        anlage.raum(21.6)  # der Raum kommt an: ein etwaiger Boost endet
        anlage.runde(2)
        anlage.wunsch(20.0)  # der Bewohner senkt wieder ab
        anlage.runde(2)
        anlage.server_antwortet(REGEL)
        anlage.vergehen(ABSTAND_HEBEN - (anlage.uhr() - beginn), REGEL, fuehler=21.6)
    starts = boost_starts(anlage)
    assert 0 < len(starts) <= MAX_STARTS_AM_TAG
    assert len(starts) == MAX_STARTS_AM_TAG  # das Limit wurde erreicht, nicht nur nicht ueberschritten
    assert all(spaeter - frueher >= MIN_ABSTAND for frueher, spaeter in zip(starts, starts[1:], strict=False))
    limit = [t for t in anlage.meldetexte() if "Tageslimit" in t]
    assert len(limit) <= 1, limit
