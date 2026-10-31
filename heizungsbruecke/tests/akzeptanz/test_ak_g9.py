"""G-9: Endzustaende. Wird die Regelung beendet (Abmelden, Abo-Ende), stehen danach die zuletzt gelernten Regelwerte
und die Heizgrenze, die vor dem ersten Schreiben stand; schlaegt das Zuruecksetzen fehl, versucht das Add-on es weiter."""
import pytest
from anlagen import LEERE_KONFIGURATION, konfiguration
from beobachtung import COMFORT_BOOST, REGELWERTE, SATZ, boost_anlage, boost_starts, stand

from smartheat_device.prozess import Neustart

URSPRUNG_HEIZGRENZE = 17.0  # nicht der Regelwert (16): so zeigt sich, welcher Wert zurueckkommt
ENDZUSTAND = {"curve": REGELWERTE["curve"], "room_setpoint": REGELWERTE["room_setpoint"],
              "heat_limit": URSPRUNG_HEIZGRENZE}
DREISSIG_TAGE = 30 * 86400


def _abgemeldet(anlage) -> bool:
    return any(m[0] == "ereignis" and m[1] == "smartheat_status" and m[2].get("status") == "abgemeldet"
               for m in anlage.ha.meldungen)


def _abmelden(anlage) -> None:
    anlage.konfiguration_senden(2, LEERE_KONFIGURATION)
    with pytest.raises(Neustart):
        anlage.runde(2)
    anlage.neu_starten()


def _abo_ende(anlage) -> None:
    anlage.konfiguration_senden(2, {**konfiguration(SATZ, "Heizkoerper"), "abo": "inaktiv"})
    anlage.laufen_ueber_neustarts(600)
    assert stand(anlage) == COMFORT_BOOST  # die Karenzzeit: das Add-on regelt im Notbetrieb weiter
    assert any("Notbetrieb" in text for text in anlage.meldetexte())
    anlage.wanduhr_vor(DREISSIG_TAGE + 10)
    anlage.laufen_ueber_neustarts(600)


@pytest.mark.akzeptanz("AK-G9-01")
@pytest.mark.parametrize("ende", ["abmelden", "abo_ende"])
def test_ak_g9_01_ende_waehrend_des_boosts_stellt_zurueck(welt, ende):
    anlage = boost_anlage(welt, heizgrenze=URSPRUNG_HEIZGRENZE)
    if ende == "abmelden":
        _abmelden(anlage)
    else:
        _abo_ende(anlage)
    assert stand(anlage) == ENDZUSTAND
    starts, schreibaufrufe = len(boost_starts(anlage)), len(anlage.ha.schreibvorgaenge)
    anlage.laufen_ueber_neustarts(3600)  # der Boost endet: nichts wird neu geschrieben, kein neuer Boost
    assert stand(anlage) == ENDZUSTAND
    assert len(anlage.ha.schreibvorgaenge) == schreibaufrufe
    assert len(boost_starts(anlage)) == starts
    if ende == "abmelden":
        assert _abgemeldet(anlage)
    else:
        assert any("Abo seit 30 Tagen inaktiv" in text for text in anlage.meldetexte())


@pytest.mark.akzeptanz("AK-G9-02")
def test_ak_g9_02_abgelehnte_rueckstellung_wird_weiter_versucht(welt):
    anlage = boost_anlage(welt, heizgrenze=URSPRUNG_HEIZGRENZE)
    anlage.ha.lehnt_ab_bis = anlage.uhr() + 7200  # HA ist zwei Stunden lang nicht erreichbar
    _abmelden(anlage)
    anlage.laufen_ueber_neustarts(3600)
    assert stand(anlage) == COMFORT_BOOST  # nichts ist zurueckgestellt
    texte = [t for t in anlage.meldetexte() if "nicht auf die zuletzt gelernten Werte" in t]
    assert texte and all(wert in texte[0] for wert in ("Heizkurve 0,9", "Zone 22", "Heizgrenze 17"))
    assert "smartheat_wiederherstellung" not in anlage.zurueckgenommen()
    anlage.laufen_ueber_neustarts(2 * 3600)
    versuche = sorted({zeit for _, _, zeit in anlage.ha.abgelehnt})
    abstaende = [round(spaeter - frueher) for frueher, spaeter in zip(versuche, versuche[1:], strict=False)]
    assert abstaende == [300, 900, 1800, 1800, 1800]  # 5, 15, 30 min, dann alle 30 min
    assert stand(anlage) == ENDZUSTAND  # HA ist wieder da: die Rueckstellung gelingt
    assert "smartheat_wiederherstellung" in anlage.zurueckgenommen()
    abgelehnt, schreibaufrufe = len(anlage.ha.abgelehnt), len(anlage.ha.schreibvorgaenge)
    anlage.laufen_ueber_neustarts(2 * 3600)  # Erfolg beendet die Versuche
    assert (len(anlage.ha.abgelehnt), len(anlage.ha.schreibvorgaenge)) == (abgelehnt, schreibaufrufe)
