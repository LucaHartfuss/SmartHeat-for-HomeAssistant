"""G-9: Endzustaende. Wird die Regelung beendet (Abmelden, Abo-Ende), stehen danach Heizkurve, Zonensollwert,
Heizgrenze und Mindestvorlauf wieder so, wie sie vor dem ersten Schreiben standen; schlaegt das Zuruecksetzen fehl,
versucht das Add-on es weiter. Die Testphase ohne Setwell ist ein Endzustand auf Zeit: der Kreis wird einmal
zurueckgestellt, danach schreibt das Add-on nichts mehr, und Ursprungswerte und Lernstand bleiben stehen. Ein Kreis
ausser Betrieb wird ebenfalls einmal zurueckgestellt; danach schreibt das Add-on nur den Kaelteschutz bei Frostnaehe
und sendet nur Raumwerte."""
import json

import pytest
from anlagen import LEERE_KONFIGURATION, konfiguration, schreibziel
from beobachtung import COMFORT_BOOST, SATZ, boost_anlage, boost_starts, stand

from heizungsbruecke import config
from smartheat_device.prozess import Neustart

URSPRUNG_HEIZGRENZE = 17.0  # nicht der Regelwert (16): so zeigt sich, welcher Wert zurueckkommt
#: Ursprung vor dem ersten Schreiben: Startwerte der Anlage (Kurve 0,9, Zonensollwert 21) und die Heizgrenze oben;
#: die gelernten Werte (Kurve 0,9, Zone 22) sind es nicht mehr
ENDZUSTAND = {"curve": 0.9, "room_setpoint": 21.0, "heat_limit": URSPRUNG_HEIZGRENZE}
DREISSIG_TAGE = 30 * 86400


def _abgemeldet(anlage) -> bool:
    return any(m[0] == "ereignis" and m[1] == "smartheat_status" and m[2].get("status") == "abgemeldet"
               for m in anlage.ha.meldungen)


def _abmelden(anlage, version: int = 2) -> None:
    anlage.konfiguration_senden(version, LEERE_KONFIGURATION)
    with pytest.raises(Neustart):
        anlage.runde(2)
    anlage.neu_starten()


def _abo_ende(anlage) -> None:
    anlage.konfiguration_senden(2, konfiguration(SATZ, "Heizkoerper", abo="inaktiv"))
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
    texte = [t for t in anlage.meldetexte() if "nicht auf die ursprünglichen Werte" in t]
    assert texte and all(wert in texte[0] for wert in ("Heizkurve 0,9", "Zone 21", "Heizgrenze 17",
                                                       "Mindestvorlauftemperatur 21"))
    assert any(m[0] == "dauerhaft" and "nicht auf die ursprünglichen Werte" in m[2] for m in anlage.ha.meldungen)  # kritisch
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


#: lokale Mindestliste der Rueckstellung je Hebelsatz (woertlich, nie aus dem Produktcode)
MINDESTLISTE = {"vaillant_vrc720": ("curve", "room_setpoint", "heat_limit", "min_flow"), "weishaupt_wwp": ("curve", "room_setpoint", "heat_limit"),
                "weishaupt_wwp_basis": ("room_setpoint",), "viessmann_vicare": ("curve", "level", "room_setpoint")}
#: Werte des Servers, die von den Startwerten der Anlage verschieden sind
GELERNT = {"vaillant_vrc720": {"curve": 0.8, "room_setpoint": 21.5, "heat_limit": 17.0, "min_flow": 22.0},
           "weishaupt_wwp": {"curve": 0.8, "room_setpoint": 21.5, "heat_limit": 17.0},
           "weishaupt_wwp_basis": {"room_setpoint": 21.5},
           "viessmann_vicare": {"curve": 0.8, "level": -2.0, "room_setpoint": 21.5}}
KUERZER = [("vaillant_vrc720", []), ("weishaupt_wwp", []), ("weishaupt_wwp", ["curve", "room_setpoint"]),
           ("weishaupt_wwp_basis", []), ("viessmann_vicare", []), ("viessmann_vicare", ["curve", "level"])]


@pytest.mark.akzeptanz("AK-G9-06")
@pytest.mark.parametrize("satz,zurueck", KUERZER, ids=[f"{s}-{'-'.join(z) or 'leer'}" for s, z in KUERZER])
def test_ak_g9_06_kuerzere_rueckstellliste_wird_abgewiesen(welt, satz, zurueck):
    anlage = welt(satz, "Heizkoerper")
    anlage.starten()
    ursprung = {hebel: anlage.ha.states[schreibziel(satz, hebel)] for hebel in MINDESTLISTE[satz]}
    anlage.server_antwortet(GELERNT[satz])
    anlage.runde(2)
    assert all(anlage.ha.states[schreibziel(satz, h)] != ursprung[h] for h in ursprung)  # die Regelung hat geschrieben
    neu = konfiguration(satz, "Heizkoerper")
    neu["bindungsparameter"]["zurueck"] = zurueck  # der Server will weniger zurueckstellen als die Mindestliste
    neu["bindungsparameter"]["zurueck_optional"] = []
    anlage.konfiguration_senden(2, neu)
    anlage.runde(2)
    antworten = [r for r in anlage.link.of("up/result") if r["dokument"] == "konfiguration" and r["version"] == 2]
    assert [(r["ok"], r["error"]["grund"], r["error"]["details"]["feld"]) for r in antworten] == [
        (False, "ungueltig", "zurueck")]  # abgewiesen, mit Feld
    _abmelden(anlage, version=3)  # das vorige Dokument gilt noch: es stellt mindestens die Mindestliste zurueck
    assert {hebel: anlage.ha.states[schreibziel(satz, hebel)] for hebel in ursprung} == ursprung


SERVER_ANDERS = {"curve": 1.1, "room_setpoint": 24.0, "heat_limit": 14.0}
SERVER_NORMAL = {"curve": 0.8, "room_setpoint": 21.0, "heat_limit": 15.0}


def _zustand_setzen(anlage, version: int, zustand: str) -> None:
    """Der Server setzt den Zustand des Kreises (ohne Neustart); das Geraet nimmt das Dokument an, der naechste lokale
    Check (hoechstens 300 s) wendet es an."""
    dokument = konfiguration(SATZ, "Heizkoerper")
    dokument["kreise"][0]["zustand"] = zustand
    anlage.konfiguration_senden(version, dokument)
    anlage.runde(2)
    assert anlage.ergebnis(version)["ok"]
    anlage.laufen(310)


def _server_antwortet_auf_neuen_wunsch(anlage, hebel: dict) -> None:
    """Ein neuer Wunsch (nicht hoeher, kein Boost) loest einen Snapshot aus; der Server antwortet darauf mit `hebel`."""
    anlage.raum(21.5)
    anlage.wunsch(21.0)
    anlage.runde(2)
    anlage.server_antwortet(hebel)
    anlage.runde(2)


def _gemerkt() -> dict:
    """Ursprungswerte und Lernwerte, wie sie auf dem Datentraeger stehen."""
    daten = json.loads(config.backup_path(1).read_text(encoding="utf-8"))
    return {feld: daten.get(feld) for feld in ("originals", "learned")}


@pytest.mark.akzeptanz("AK-G9-07")
def test_ak_g9_07_testphase_ohne_stellt_einmal_zurueck_und_schreibt_dann_nichts(welt):
    anlage = boost_anlage(welt, heizgrenze=URSPRUNG_HEIZGRENZE)
    _zustand_setzen(anlage, 2, "testphase_ohne")
    assert stand(anlage) == ENDZUSTAND  # einmal zurueckgestellt, der Boost ist beendet
    schreibaufrufe, starts = len(anlage.ha.schreibvorgaenge), len(boost_starts(anlage))
    snapshots, telemetrie = len(anlage.link.of("up/snapshot")), len(anlage.link.of("telemetry"))
    anlage.wunsch(23.0)  # der Wunsch steigt bei kaltem Raum: sonst ein Comfort-Boost
    anlage.app_verstellt(schreibziel(SATZ, "heat_limit"), 12.0)  # ein Hebel wird von aussen verstellt: kein Durchsetzen
    anlage.vergehen(3600, SERVER_ANDERS, fuehler=20.0)  # der Server antwortet jedes Mal mit anderen Werten
    assert len(anlage.ha.schreibvorgaenge) == schreibaufrufe
    assert len(boost_starts(anlage)) == starts
    assert anlage.ha.states[schreibziel(SATZ, "heat_limit")] == 12.0
    assert any(m[0] == "ereignis" and m[1] == "smartheat_status" for m in anlage.ha.meldungen)  # die Meldungen laufen
    assert len(anlage.link.of("up/snapshot")) > snapshots  # Snapshots und Telemetrie laufen in der Testphase weiter
    neu = anlage.link.of("telemetry")[telemetrie:]
    assert neu and all(paket["testphase_ohne"] is True for paket in neu)  # der Server nimmt die Zeiten aus der Auswertung


@pytest.mark.akzeptanz("AK-G9-08")
def test_ak_g9_08_zweite_testphase_stellt_auf_die_ursprungswerte_der_ersten_zurueck(welt):
    anlage = boost_anlage(welt, heizgrenze=URSPRUNG_HEIZGRENZE)
    _zustand_setzen(anlage, 2, "testphase_ohne")
    assert stand(anlage) == ENDZUSTAND
    _zustand_setzen(anlage, 3, "in_betrieb")  # zurueck ohne Neustart: die Regelung nimmt Serverwerte wieder auf
    _server_antwortet_auf_neuen_wunsch(anlage, SERVER_NORMAL)
    assert stand(anlage) == SERVER_NORMAL
    _zustand_setzen(anlage, 4, "testphase_ohne")
    assert stand(anlage) == ENDZUSTAND  # die Werte vor dem ersten Schreiben, nicht die der Zeit dazwischen
    schreibaufrufe = len(anlage.ha.schreibvorgaenge)
    anlage.vergehen(1800, SERVER_ANDERS, fuehler=21.5)
    assert len(anlage.ha.schreibvorgaenge) == schreibaufrufe


@pytest.mark.akzeptanz("AK-G9-09")
def test_ak_g9_09_ursprungswerte_und_lernstand_bleiben_nach_der_testphase_gleich(welt):
    anlage = boost_anlage(welt, heizgrenze=URSPRUNG_HEIZGRENZE)
    vorher = _gemerkt()
    assert vorher["originals"]
    _zustand_setzen(anlage, 2, "testphase_ohne")
    anlage.vergehen(3600, SERVER_ANDERS, fuehler=21.5)
    assert _gemerkt() == vorher  # nichts geleert, nichts neu gemerkt, kein neuer Lernstand
    _zustand_setzen(anlage, 3, "in_betrieb")
    _server_antwortet_auf_neuen_wunsch(anlage, SERVER_NORMAL)
    assert stand(anlage) == SERVER_NORMAL  # die Regelung schreibt wieder
    assert _gemerkt()["originals"] == vorher["originals"]
    _abmelden(anlage, version=4)  # das Ende nach der Testphase stellt auf die Ursprungswerte von ganz am Anfang zurueck
    assert stand(anlage) == ENDZUSTAND


#: Hebelwerte im Kaelteschutz (Vaillant x Heizkoerper), wie beim Notfall-Boost
KAELTESCHUTZ = {"curve": 1.5, "room_setpoint": 25.0, "heat_limit": 23.0}
TELEMETRIE_NUR_RAUM = {"kreis", "room_actual", "boost_active", "failsafe_active", "ts", "raeume"}


@pytest.mark.akzeptanz("AK-G9-10")
def test_ak_g9_10_ausser_betrieb_schreibt_nur_den_kaelteschutz_bei_frostnaehe(welt):
    anlage = boost_anlage(welt, heizgrenze=URSPRUNG_HEIZGRENZE)
    _zustand_setzen(anlage, 2, "ausser_betrieb")
    assert stand(anlage) == ENDZUSTAND  # einmal zurueckgestellt, der Boost ist beendet
    schreibaufrufe, starts = len(anlage.ha.schreibvorgaenge), len(boost_starts(anlage))
    anlage.wunsch(23.0)
    anlage.app_verstellt(schreibziel(SATZ, "heat_limit"), 12.0)  # von aussen verstellt: nichts wird durchgesetzt
    anlage.vergehen(3 * 3600, SERVER_ANDERS, fuehler=18.0)  # drei Stunden unter dem Wunsch: kein Soll-Ausloeser
    assert len(anlage.ha.schreibvorgaenge) == schreibaufrufe and len(boost_starts(anlage)) == starts
    assert anlage.ha.states[schreibziel(SATZ, "heat_limit")] == 12.0
    anlage.vergehen(600, SERVER_ANDERS, fuehler=4.9)  # unter 5 Grad, aber erst zehn Minuten: noch kein Eingriff
    assert len(anlage.ha.schreibvorgaenge) == schreibaufrufe
    anlage.vergehen(600, SERVER_ANDERS, fuehler=4.9)  # ueber 15 Minuten unter 5 Grad: der Kaelteschutz hebt an
    assert stand(anlage) == KAELTESCHUTZ
    anlage.vergehen(600, SERVER_ANDERS, fuehler=6.0)  # der Lauf haelt bis zur Ankunft
    assert stand(anlage) == KAELTESCHUTZ
    anlage.raum(7.0)  # der Raum ist wieder bei 7 Grad: der Lauf endet auf den Ursprungswerten
    anlage.runde(2)
    assert stand(anlage) == ENDZUSTAND
    fertig = len(anlage.ha.schreibvorgaenge)
    anlage.vergehen(3600, SERVER_ANDERS, fuehler=8.0)
    assert len(anlage.ha.schreibvorgaenge) == fertig  # kein neuer Lauf ohne neue Frostnaehe


@pytest.mark.akzeptanz("AK-G9-11")
def test_ak_g9_11_ausser_betrieb_sendet_nur_raumwerte(welt):
    anlage = boost_anlage(welt, heizgrenze=URSPRUNG_HEIZGRENZE)
    _zustand_setzen(anlage, 2, "ausser_betrieb")
    snapshots, telemetrie = len(anlage.link.of("up/snapshot")), len(anlage.link.of("telemetry"))
    anlage.wunsch(23.0)  # ein Wunsch loest sonst einen Snapshot aus
    anlage.vergehen(3600, SERVER_ANDERS, fuehler=20.0)
    neu = anlage.link.of("telemetry")[telemetrie:]
    assert neu and all(set(paket) == TELEMETRIE_NUR_RAUM for paket in neu)  # keine Hebel- und Regelungsdaten
    assert all(paket["boost_active"] is False and paket["failsafe_active"] is False for paket in neu)
    assert len(anlage.link.of("up/snapshot")) == snapshots
    _zustand_setzen(anlage, 3, "in_betrieb")  # zurueck ohne Neustart: Snapshots laufen wieder
    _server_antwortet_auf_neuen_wunsch(anlage, SERVER_NORMAL)
    assert len(anlage.link.of("up/snapshot")) > snapshots and stand(anlage) == SERVER_NORMAL


def _kreis_im_status(anlage) -> dict:
    """Der Eintrag des Kreises im zuletzt gemeldeten Status-Ereignis (das Home Assistant der Integration zeigt)."""
    ereignisse = [m[2] for m in anlage.ha.meldungen if m[0] == "ereignis" and m[1] == "smartheat_status"]
    return ereignisse[-1]["kreise"][0]


@pytest.mark.akzeptanz("AK-G9-12")
def test_ak_g9_12_status_zeigt_testphase_ausser_betrieb_und_kaelteschutz_je_kreis(welt):
    anlage = boost_anlage(welt, heizgrenze=URSPRUNG_HEIZGRENZE)
    assert (_kreis_im_status(anlage)["zustand"], _kreis_im_status(anlage)["kaelteschutz"]) == ("regelt", "aus")
    _zustand_setzen(anlage, 2, "testphase_ohne")
    assert _kreis_im_status(anlage)["zustand"] == "testphase"  # ohne Neustart, mit dem naechsten lokalen Check
    _zustand_setzen(anlage, 3, "ausser_betrieb")
    assert _kreis_im_status(anlage)["zustand"] == "ausser_betrieb"
    anlage.vergehen(1200, SERVER_ANDERS, fuehler=4.9)  # ueber 15 Minuten unter 5 Grad: der Kaelteschutz hebt an
    assert stand(anlage) == KAELTESCHUTZ
    assert _kreis_im_status(anlage) == {**_kreis_im_status(anlage), "zustand": "ausser_betrieb", "kaelteschutz": "frost"}
    anlage.raum(7.0)  # der Lauf endet
    anlage.runde(2)
    assert _kreis_im_status(anlage)["kaelteschutz"] == "aus"
    _zustand_setzen(anlage, 4, "in_betrieb")
    assert _kreis_im_status(anlage)["zustand"] == "regelt"
