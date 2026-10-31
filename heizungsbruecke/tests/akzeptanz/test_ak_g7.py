"""Zugang zur Hersteller-Cloud: verstellt die App eine Einstellung immer wieder, setzt das Add-on sie nur begrenzt oft
und mit Abstand zurueck (Abrufkontingent der Cloud). Die Grenzen stehen hier woertlich, nie aus dem Produktcode."""
import pytest
from anlagen import schreibziel

REGELWERTE = {"curve": 0.8, "room_setpoint": 21.5, "heat_limit": 17.0}
VERSTELLUNGEN = 8
#: wartet bis alles eingeschwungen ist (Anlage meldet eigene Schreibvorgaenge erst nach 2100 s) und der Check zweimal lief
EINSCHWINGEN = 2700
#: die App verstellt schneller, als das Add-on zuruecksetzen darf (Mindestabstand 1800 s): der Abstand der Versuche
#: kommt vom Add-on, nicht vom Takt der App
ABSTAND_DER_APP = 1500
LIMIT_TEXT = "Abrufkontingent"


@pytest.mark.akzeptanz("AK-G7-01")
def test_ak_g7_01_app_verstellt_die_kurve_achtmal_am_tag(welt):
    anlage = welt("vaillant_vrc720", "Heizkoerper")
    anlage.konfiguration["tagestick"] = "23:00"  # kein Tagestick im Fenster: der Server fordert die Werte nicht erneut
    anlage.konfiguration["bindung"]["benachrichtigung"] = ["notify.mobile_app_handy"]  # der Bewohner bekommt Push
    anlage.starten()
    anlage.runde(2)
    anlage.server_antwortet(REGELWERTE)
    anlage.vergehen(EINSCHWINGEN, REGELWERTE, fuehler=22.0)  # die Werte sind geschrieben und eingeschwungen
    kurve = schreibziel("vaillant_vrc720", "curve")
    serverwert = [(w, t) for e, w, t in anlage.ha.schreibvorgaenge if e == kurve]
    assert [w for w, _ in serverwert] == [0.8]
    anlage.ha.lehnt_ab_bis = anlage.uhr() + 86400  # HA lehnt jedes Zuruecksetzen ab: jeder Versuch bleibt sichtbar
    for nr in range(VERSTELLUNGEN):  # die App ist hartnaeckig: jedes Mal ein anderer Wert als die Regelwerte
        anlage.app_verstellt(kurve, 1.5 - 0.1 * (nr % 2))
        anlage.vergehen(ABSTAND_DER_APP, REGELWERTE, fuehler=22.0)
    versuche = [t for e, w, t in anlage.ha.abgelehnt if e == kurve and w == 0.8]
    assert len(versuche) == 6  # nicht mehr als sechs Rueckschreibversuche am Tag
    assert all(spaeter - frueher >= 1800 for frueher, spaeter in zip(versuche, versuche[1:], strict=False))
    assert anlage.ha.states[kurve] != 0.8  # die Verstellung der App bleibt stehen
    limit = [text for text in anlage.meldetexte() if LIMIT_TEXT in text]
    assert len(limit) == 1  # genau eine Meldung, dass heute nicht mehr zurueckgesetzt wird
    assert anlage.meldetexte()[-1] == limit[0]  # sie kam nach dem sechsten Versuch, danach nichts mehr
