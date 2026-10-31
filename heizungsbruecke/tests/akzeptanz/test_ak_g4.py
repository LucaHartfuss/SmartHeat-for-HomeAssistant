"""Ende des Comfort-Boosts: spaetestens nach 4 h und beim dritten unlesbaren Raumwert in Folge. Die Grenzen stehen
hier woertlich, nie aus dem Produktcode."""
import pytest
from beobachtung import COMFORT_BOOST, REGELWERTE, boost_anlage, boost_starts, stand

VIER_STUNDEN = 4 * 3600


@pytest.mark.akzeptanz("AK-G4-01")
@pytest.mark.parametrize("wanduhr_zurueck", [False, True], ids=["wanduhr-stetig", "wanduhr-springt-zurueck"])
def test_ak_g4_01_boost_endet_nach_vier_stunden(welt, wanduhr_zurueck):
    anlage = boost_anlage(welt)
    beginn = boost_starts(anlage)[0]
    anlage.vergehen(3600 - (anlage.uhr() - beginn), REGELWERTE, fuehler=19.0)
    if wanduhr_zurueck:  # die Systemzeit wird um eine halbe Stunde zurueckgestellt: die Wanduhr zeigt nur 30 min
        anlage.wanduhr_zurueck(1800)
    anlage.vergehen(VIER_STUNDEN - 60 - (anlage.uhr() - beginn), REGELWERTE, fuehler=19.0)  # 3 h 59 min
    assert stand(anlage) == COMFORT_BOOST
    anlage.vergehen(59, REGELWERTE, fuehler=19.0, takt=1)  # bis eine Sekunde vor 4 h, jede Sekunde ein Check
    assert anlage.uhr() - beginn == VIER_STUNDEN - 1
    anlage.vergehen(1, REGELWERTE, fuehler=19.0, takt=1)  # der Check bei 3 h 59 min 59 s
    assert stand(anlage) == COMFORT_BOOST
    anlage.vergehen(1, REGELWERTE, fuehler=19.0, takt=1)  # der Check bei 4 h
    assert stand(anlage) == REGELWERTE
    assert len(boost_starts(anlage)) == 1  # kein neuer Boost: der Wunsch wurde nicht erneut erhoeht


@pytest.mark.akzeptanz("AK-G4-02")
def test_ak_g4_02_boost_endet_beim_dritten_unlesbaren_raumwert(welt):
    anlage = boost_anlage(welt)
    anlage.raum_unlesbar()  # erster Fehlwert
    anlage.runde(2)
    anlage.server_antwortet(REGELWERTE)
    anlage.raum(19.0)  # dazwischen ein lesbarer Wert: die Zaehlung beginnt neu
    anlage.runde(2)
    for _ in range(2):  # zwei Fehlwerte in Folge: der Boost bleibt
        anlage.raum_unlesbar()
        anlage.runde(2)
        anlage.server_antwortet(REGELWERTE)
        assert stand(anlage) == COMFORT_BOOST
    anlage.raum_unlesbar()  # der dritte in Folge
    anlage.runde(2)
    assert stand(anlage) == REGELWERTE
    assert len(boost_starts(anlage)) == 1
