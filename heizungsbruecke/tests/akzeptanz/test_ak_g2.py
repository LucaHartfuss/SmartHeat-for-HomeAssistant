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
    """Zahl der Schreibvorgaenge auf die drei Hebel (der Mindestvorlauf, ein vierter Hebel des Servers, bleibt aussen vor)."""
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


SERVER = {"curve": 0.8, "room_setpoint": 21.0, "heat_limit": 15.0}  # was der Server fuer Soll 21 fordert
STUNDE = 3600


def _warm_gestartet(welt) -> tuple[Anlage, str]:
    """Vaillant x Heizkoerper bei Soll 21,0, Raum warm, Serverwerte angekommen."""
    satz = "vaillant_vrc720"
    anlage: Anlage = welt(satz, "Heizkoerper")
    anlage.ha.states[RAUMFUEHLER] = 21.0
    anlage.starten()
    anlage.server_antwortet(SERVER)
    anlage.runde(2)
    assert _stand(anlage, satz) == SERVER
    return anlage, satz


@pytest.mark.akzeptanz("AK-G2-05")
def test_ak_g2_05_kaelteschutz_bei_langer_abweichung_vom_soll(welt):
    anlage, satz = _warm_gestartet(welt)
    anlage.vergehen(4 * STUNDE, SERVER, 19.1)  # 1,9 K zu kalt, auch nach Stunden: unter der Schwelle, kein Eingriff
    assert _stand(anlage, satz) == SERVER
    anlage.vergehen(2 * STUNDE + 600, SERVER, 18.9)  # 2,1 K zu kalt, aber noch nicht drei Stunden
    assert _stand(anlage, satz) == SERVER
    anlage.vergehen(STUNDE, SERVER, 18.9)  # drei Stunden ueber der Schwelle: der Kaelteschutz hebt die Anlage an
    assert _stand(anlage, satz) == COMFORT_BOOST
    geschrieben = _schreibungen(anlage, satz)
    anlage.vergehen(STUNDE, SERVER, 19.5)  # der Server antwortet weiter mit seinen Werten, der Lauf haelt bis zur Ankunft
    assert _stand(anlage, satz) == COMFORT_BOOST and _schreibungen(anlage, satz) == geschrieben
    anlage.raum(20.4)  # noch unter Soll - 0,5
    anlage.runde(2)
    assert _stand(anlage, satz) == COMFORT_BOOST
    anlage.raum(20.5)  # Soll - 0,5 erreicht: Rueckkehr auf die Werte des Servers
    anlage.runde(2)
    assert _stand(anlage, satz) == SERVER


@pytest.mark.akzeptanz("AK-G2-07")
def test_ak_g2_07_konstanter_frostwert_bei_verbundenem_trigger_startet_den_kaelteschutz_nach_15_minuten(welt):
    anlage, satz = _warm_gestartet(welt)
    assert anlage.trigger[-1].connected  # HA ist verbunden: der Watchdog allein haette bisher nichts geprueft
    anlage.raum(4.0)  # der Fuehler meldet einmal Frostnaehe; danach feuert HA bei konstantem Wert kein Ereignis mehr
    for _ in range(14):  # 14 Minuten
        anlage.server_antwortet(SERVER)
        anlage.runde(60)
    assert _stand(anlage, satz) == SERVER  # noch keine 15 Minuten
    for _ in range(6):  # ein weiterer Watchdog-Takt (fuenf Minuten) liegt nach der 15. Minute
        anlage.server_antwortet(SERVER)
        anlage.runde(60)
    assert _stand(anlage, satz) == COMFORT_BOOST  # der Kaelteschutz hebt die Anlage an, obwohl sich nichts mehr aenderte


@pytest.mark.akzeptanz("AK-G2-06")
def test_ak_g2_06_ausgefallener_fuehler_loest_nichts_aus_und_beendet_nichts(welt):
    anlage, satz = _warm_gestartet(welt)
    anlage.raum_unlesbar()  # der Fuehler faellt aus: aus einem Ausfall allein folgt kein Eingriff
    for _ in range(6 * 60):
        anlage.server_antwortet(SERVER)
        anlage.runde(60)
    assert _stand(anlage, satz) == SERVER
    anlage.vergehen(3 * STUNDE + 600, SERVER, 18.5)  # kalt genug, lange genug: der Lauf beginnt
    assert _stand(anlage, satz) == COMFORT_BOOST
    anlage.raum_unlesbar()  # der Fuehler faellt waehrend des Laufs aus: der Lauf bleibt
    for _ in range(60):
        anlage.server_antwortet(SERVER)
        anlage.runde(60)
    assert _stand(anlage, satz) == COMFORT_BOOST
    anlage.raum(21.0)  # der Fuehler ist zurueck und zeigt die Ankunft
    anlage.runde(2)
    assert _stand(anlage, satz) == SERVER


KONTROLLRAUM = "sensor.room_actual_2"


@pytest.mark.akzeptanz("AK-G2-08")
def test_ak_g2_08_frost_im_kontrollraum_bei_warmem_referenzraum_haelt_den_lauf_bis_alle_raeume_7_grad_haben(welt):
    satz = "vaillant_vrc720"
    anlage: Anlage = welt(satz, "Heizkoerper")
    anlage.konfiguration["kreise"][0]["raeume"].append(
        {"raum": 2, "name": "Schlafzimmer", "rolle": "kontroll", "fuehler": [KONTROLLRAUM], "soll_quelle": False,
         "versatz_k": 0.0, "ziel_fest": None})
    anlage.ha.states[RAUMFUEHLER] = 21.0
    anlage.ha.states[KONTROLLRAUM] = 21.0
    anlage.ha.states[f"{KONTROLLRAUM}::device_class"] = "temperature"
    anlage.starten()  # Soll 21,0, beide Raeume warm
    anlage.server_antwortet(SERVER)
    anlage.runde(2)
    assert _stand(anlage, satz) == SERVER
    anlage.ha.states[KONTROLLRAUM] = 4.9  # der Kontrollraum friert, der Referenzraum bleibt bei Soll
    anlage.vergehen(20 * 60, SERVER, 21.0)
    assert _stand(anlage, satz) == COMFORT_BOOST  # nach 15 Minuten unter 5 Grad hebt der Kaelteschutz an
    geschrieben = _schreibungen(anlage, satz)
    for _ in range(6):  # eine Stunde: der Lauf bleibt, ohne Rueckkehr und neuen Start
        anlage.vergehen(10 * 60, SERVER, 21.0)
        assert _stand(anlage, satz) == COMFORT_BOOST and _schreibungen(anlage, satz) == geschrieben
    anlage.ha.states[KONTROLLRAUM] = 6.9  # noch unter 7 Grad
    anlage.vergehen(10 * 60, SERVER, 21.0)
    assert _stand(anlage, satz) == COMFORT_BOOST
    anlage.ha.states[KONTROLLRAUM] = 7.0  # alle Raeume mindestens 7 Grad: zurueck auf die Werte des Servers
    anlage.vergehen(10 * 60, SERVER, 21.0)
    assert _stand(anlage, satz) == SERVER


@pytest.mark.akzeptanz("AK-G2-09")
def test_ak_g2_09_neustart_zwischendurch_setzt_die_frostnaehe_zaehlung_nicht_zurueck(welt):
    anlage, satz = _warm_gestartet(welt)
    anlage.raum(4.0)
    anlage.laufen(10 * 60, takt=60)  # zehn Minuten unter 5 Grad: noch kein Lauf
    assert _stand(anlage, satz) == SERVER
    anlage.neu_starten()  # der Prozess startet mitten in der Zaehlung neu, der Fuehler meldet weiter 4 Grad
    anlage.raum(4.0)
    anlage.laufen(10 * 60, takt=60)  # 20 Minuten seit dem Beginn der Kaelte, aber nur 10 seit dem Neustart
    assert _stand(anlage, satz) == COMFORT_BOOST


@pytest.mark.akzeptanz("AK-G2-10")
def test_ak_g2_10_sprung_der_wanduhr_verlaengert_oder_verkuerzt_den_kaelteschutz_nicht(welt):
    anlage, satz = _warm_gestartet(welt)
    anlage.vergehen(3 * STUNDE + 10 * 60, SERVER, 18.5)  # 2,5 K zu kalt, drei Stunden: der Lauf beginnt
    assert _stand(anlage, satz) == COMFORT_BOOST
    anlage.wanduhr_vor(5 * STUNDE)  # die Systemzeit springt um fuenf Stunden vor: kein Ende nach der Wanduhr
    anlage.vergehen(60, SERVER, 18.5)
    assert _stand(anlage, satz) == COMFORT_BOOST
    anlage.wanduhr_zurueck(8 * STUNDE)  # und danach weit zurueck: auch das beendet den Lauf nicht
    anlage.vergehen(3 * STUNDE + 20 * 60, SERVER, 18.5)  # etwa 3 h 30 min nach dem Start des Laufs
    assert _stand(anlage, satz) == COMFORT_BOOST
    anlage.vergehen(40 * 60, SERVER, 18.5)  # vier Stunden nach dem Start, gemessen an der laufenden Uhr
    assert _stand(anlage, satz) == SERVER
