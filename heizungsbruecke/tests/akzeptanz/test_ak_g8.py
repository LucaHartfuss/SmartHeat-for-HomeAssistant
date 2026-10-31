"""Kontrolle: eine Verstellung in der Hersteller-App wird nach zwei Pruefrunden zurueckgesetzt (mit einer Meldung), und
der Wunsch am Thermostat wird nie geschrieben. Die Grenzen stehen hier woertlich, nie aus dem Produktcode."""
import pytest
from anlagen import LEERE_KONFIGURATION, RAUMFUEHLER, schreibziel
from beobachtung import COMFORT_BOOST, REGELWERTE, SATZ, boost_anlage, stand
from welt_handgriffe import ROOM_TARGET

from smartheat_device.prozess import Neustart

EINSCHWINGEN = 2700  # Vaillant: eigene Schreibvorgaenge gelten erst nach 2100 s als eingeschwungen
PRUEFINTERVALL = 300
APP_WERT = 1.2
VERSTELLT = "in der App verstellt"


def _geschrieben(anlage, entity: str) -> list[tuple[object, float]]:
    return [(wert, zeit) for ziel, wert, zeit in anlage.ha.schreibvorgaenge if ziel == entity]


@pytest.mark.akzeptanz("AK-G8-01")
def test_ak_g8_01_verstellung_in_der_app_wird_nach_zwei_runden_zurueckgesetzt(welt):
    anlage = welt(SATZ, "Heizkoerper")
    anlage.konfiguration["bindung"]["benachrichtigung"] = ["notify.mobile_app_handy"]  # der Bewohner bekommt Push
    anlage.starten()
    anlage.runde(2)
    anlage.server_antwortet(REGELWERTE)
    anlage.vergehen(EINSCHWINGEN, REGELWERTE, fuehler=22.0)  # die Werte sind geschrieben und eingeschwungen
    kurve = schreibziel(SATZ, "curve")
    assert anlage.ha.states[kurve] == REGELWERTE["curve"]
    anlage.laufen(PRUEFINTERVALL // 2)  # mitten zwischen zwei Pruefrunden
    beginn = anlage.uhr()
    anlage.app_verstellt(kurve, APP_WERT)
    anlage.laufen(3 * PRUEFINTERVALL)  # drei Pruefintervalle: die erste Runde sieht die Abweichung nur
    zurueck = [zeit for wert, zeit in _geschrieben(anlage, kurve) if wert == REGELWERTE["curve"] and zeit > beginn]
    assert zurueck, "die Verstellung blieb stehen"
    assert PRUEFINTERVALL < zurueck[0] - beginn <= 2 * PRUEFINTERVALL  # frueher als nach der zweiten Runde nicht
    assert anlage.ha.states[kurve] == REGELWERTE["curve"]
    meldungen = [m for m in anlage.ha.meldungen if VERSTELLT in (m[2] if len(m) > 2 else "")]
    assert [m[0] for m in meldungen] == ["push"]  # genau eine Meldung, nicht kritisch: kein Dauer-Hinweis in HA


def _ohne_wunsch(hebel: dict) -> dict:
    """Die Hebel ohne die Raum-Solltemperatur der Anlage (sie ist der Gegenstand der Pruefung am Ende)."""
    return {name: wert for name, wert in hebel.items() if name != "room_setpoint"}


def _am_thermostat(anlage) -> list[tuple[str, object, float]]:
    """Schreibaufrufe auf die Wunsch-Entity (auch auf ihre Attribute)."""
    return [vorgang for vorgang in anlage.ha.schreibvorgaenge if vorgang[0].partition("::")[0] == ROOM_TARGET]


@pytest.mark.akzeptanz("AK-G8-02")
def test_ak_g8_02_der_wunsch_am_thermostat_wird_nie_geschrieben(welt):
    anlage = welt(SATZ, "Heizkoerper")
    anlage.konfiguration["bindung"]["benachrichtigung"] = ["notify.mobile_app_handy"]
    anlage.ha.states[RAUMFUEHLER] = 21.0
    anlage.starten()
    anlage.server_antwortet({"curve": 0.8, "room_setpoint": 21.0, "heat_limit": 15.0})
    anlage.runde(2)
    ziel = schreibziel(SATZ, "room_setpoint")
    kurve = schreibziel(SATZ, "curve")
    # Boost: der Raum ist kalt, der Bewohner stellt 23 Grad
    anlage.raum(19.0)
    anlage.wunsch(23.0)
    anlage.runde(2)
    anlage.server_antwortet(REGELWERTE)
    anlage.runde(2)
    assert _ohne_wunsch(stand(anlage)) == _ohne_wunsch(COMFORT_BOOST)
    anlage.raum(23.5)  # der Raum ist warm: der Boost endet, die Regelwerte gelten wieder
    anlage.runde(2)
    assert _ohne_wunsch(stand(anlage)) == _ohne_wunsch(REGELWERTE)
    # Durchsetzen: die App verstellt die Kurve und die Verschiebung, nach dem Einschwingen setzt das Add-on zurueck
    anlage.vergehen(EINSCHWINGEN, REGELWERTE, fuehler=23.5)
    anlage.app_verstellt(kurve, APP_WERT)
    anlage.app_verstellt(ziel, 24.0)
    anlage.laufen(3 * PRUEFINTERVALL)
    assert _ohne_wunsch(stand(anlage)) == _ohne_wunsch(REGELWERTE)
    assert any(VERSTELLT in text for text in anlage.meldetexte())
    # Notbetrieb: der Server schweigt, der Raum ist weit unter dem Wunsch
    anlage.wunsch(22.5)
    anlage.runde(2)
    anlage.raum(20.0)
    anlage.laufen(75)
    assert any("Notbetrieb aktiv" in text for text in anlage.meldetexte())
    anlage.raum(19.0)
    anlage.runde(2)
    assert _ohne_wunsch(stand(anlage)) == _ohne_wunsch(COMFORT_BOOST)  # der Notfall-Boost schreibt die Hebel
    # Abmelden: das Add-on stellt zurueck und startet neu
    anlage.konfiguration_senden(2, LEERE_KONFIGURATION)
    with pytest.raises(Neustart):
        anlage.runde(2)
    assert anlage.ergebnis(2) == {"ok": True, "grund": None}
    assert _am_thermostat(anlage) == []  # die Wunsch-Entity bekam nie einen Schreibaufruf
    assert anlage.ha.states[ROOM_TARGET] == 22.5  # und steht noch auf dem Wunsch des Bewohners
    assert len([w for e, w, _ in anlage.ha.schreibvorgaenge if e == ziel]) >= 4  # der Hebel wurde in jeder Stufe benutzt


@pytest.mark.akzeptanz("AK-G8-03")
def test_ak_g8_03_entfernen_ueber_stdin_stellt_zurueck_fremde_id_nicht(welt):
    anlage = boost_anlage(welt, heizgrenze=17.0)
    anlage.stdin_sendet(b"entfernen shg-fremd\n")  # ein anderes Geraet: nichts geschieht
    neustart = False
    try:
        anlage.laufen(120)
    except Neustart:
        neustart = True
    assert not neustart and stand(anlage) == COMFORT_BOOST
    anlage.stdin_sendet(f"entfernen {anlage.geraete_id()}\n".encode())  # der geloeschte Eintrag dieses Geraets
    with pytest.raises(Neustart):
        anlage.runde(2)
    anlage.neu_starten()
    assert stand(anlage) == {"curve": 0.9, "room_setpoint": 22.0, "heat_limit": 17.0}
