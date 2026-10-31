"""Schreibverschleiss: ein unveraenderter Wert kostet keinen Schreibvorgang, das Tagesbudget des Weishaupt-Reglers
(EEPROM) und die Obergrenzen der Konfiguration. Die Grenzen stehen hier woertlich, nie aus dem Produktcode."""
import pytest
from anlagen import ALLE, hebel_von, konfiguration, schreibziel

TICKS = 20
#: Werte, die der Server fordert: innerhalb der Grenzen und von den Startwerten der Anlage verschieden
FORDERUNG = {
    ("vaillant_vrc720", "Heizkoerper"): {"curve": 0.8, "room_setpoint": 21.5, "heat_limit": 17.0},
    ("weishaupt_wwp", "Heizkoerper"): {"curve": 0.8, "room_setpoint": 21.5, "heat_limit": 17.0},
    ("weishaupt_wwp_basis", "Heizkoerper"): {"room_setpoint": 21.5},
    ("viessmann_vicare", "Heizkoerper"): {"curve": 0.8, "level": -2.0, "room_setpoint": 21.5},
    ("vaillant_vrc720", "Fussbodenheizung"): {"curve": 0.3, "room_setpoint": 21.5, "heat_limit": 12.0},
    ("weishaupt_wwp", "Fussbodenheizung"): {"curve": 0.2, "room_setpoint": 21.5, "heat_limit": 12.0},
    ("weishaupt_wwp_basis", "Fussbodenheizung"): {"room_setpoint": 21.5},
    ("viessmann_vicare", "Fussbodenheizung"): {"curve": 0.35, "level": -2.0, "room_setpoint": 21.5},
}


TAG = 86400


def tagestick(anlage, forderung: dict) -> None:
    """Ein Tag vergeht; der lokale Check (nach dem Verbindungsaufbau zu HA) macht den faelligen Tagestick, der Server
    antwortet mit `forderung`."""
    anlage.runde(TAG)
    anlage.ha_verbindet()
    anlage.laufen(10, takt=2)
    anlage.server_antwortet(forderung)
    anlage.runde(2)


@pytest.mark.akzeptanz("AK-G6-01")
@pytest.mark.parametrize("satz,verteilsystem", ALLE, ids=[f"{s}-{v}" for s, v in ALLE])
def test_ak_g6_01_unveraenderte_werte_kosten_keinen_schreibvorgang(welt, satz, verteilsystem):
    forderung = FORDERUNG[(satz, verteilsystem)]
    assert set(forderung) == set(hebel_von(satz))
    anlage = welt(satz, verteilsystem)
    anlage.starten()
    anlage.runde(4 * 3600)  # 13:00: der erste Tagestick
    anlage.runde(2)
    anlage.server_antwortet(forderung)
    anlage.runde(2)
    nach_dem_ersten = list(anlage.ha.schreibvorgaenge)
    assert nach_dem_ersten  # die Werte kamen an
    for _ in range(1, TICKS):  # derselbe Satz Werte, immer neue Ticks
        tagestick(anlage, forderung)
    assert len({x["seq"] for x in anlage.link.of("up/snapshot")}) == TICKS  # 20 Ticks (Wiederholungen: gleiche seq)
    assert anlage.ha.schreibvorgaenge == nach_dem_ersten  # kein weiterer Schreibvorgang
    for hebel in forderung:
        ziel = schreibziel(satz, hebel)
        assert len([w for e, w, _ in anlage.ha.schreibvorgaenge if e == ziel]) == 1, hebel  # genau einer je Hebel


def _fuenfzehn_werte(anlage) -> list:
    """Ein Tag mit 15 verschiedenen Kurvenwerten (0,30 ... 1,00) vom Server; jeder Wunschwechsel gibt einen Tick.
    Liefert die Schreibvorgaenge der Fake-HA."""
    for nr in range(15):
        anlage.raum(22.0)  # warm: der Wunschwechsel startet keinen Boost
        anlage.wunsch(21.5 if nr % 2 else 21.0)
        anlage.runde(2)
        anlage.server_antwortet({"curve": round(0.3 + 0.05 * nr, 2), "room_setpoint": 21.5, "heat_limit": 17.0})
        anlage.runde(300)
    assert len({x["seq"] for x in anlage.link.of("up/snapshot")}) == 15  # alle 15 Antworten sind eingegangen
    return list(anlage.ha.schreibvorgaenge)


@pytest.mark.akzeptanz("AK-G6-02")
def test_ak_g6_02_weishaupt_schreibt_hoechstens_zehnmal_am_tag(welt):
    anlage = welt("weishaupt_wwp", "Heizkoerper")
    anlage.konfiguration["bindung"]["benachrichtigung"] = ["notify.mobile_app_handy"]  # der Bewohner bekommt Push
    anlage.starten()
    kurve = schreibziel("weishaupt_wwp", "curve")
    heute = _fuenfzehn_werte(anlage)
    assert 0 < len(heute) <= 10  # das Tageslimit des Geraetespeichers
    assert [w for e, w, _ in heute if e == kurve][-1] < 1.0  # der letzte Kurvenwert fehlt noch
    assert len([t for t in anlage.meldetexte() if "Tageslimit" in t]) == 1  # eine Meldung, nicht je abgewiesenem Wert
    anlage.runde(TAG)  # der Folgetag: der letzte Kurvenwert wird nachgeholt
    anlage.laufen(600, takt=5)
    danach = anlage.ha.schreibvorgaenge[len(heute):]
    assert [(e, w) for e, w, _ in danach] == [(kurve, 1.0)]
    assert len([t for t in anlage.meldetexte() if "Tageslimit" in t]) == 1  # am Folgetag keine neue Meldung


def _tagesbudget_1000(dokument: dict) -> None:
    dokument["bindungsparameter"]["schreibschutz"]["tag"] = 1000


def _schreibgruppe_mit_sollwert(dokument: dict) -> None:
    dokument["bindungsparameter"]["schreibgruppen"] = [["curve", "level", "room_setpoint"]]


@pytest.mark.akzeptanz("AK-G6-03")
@pytest.mark.parametrize("satz,aendern,fehler", [
    ("weishaupt_wwp", _tagesbudget_1000,
     {"grund": "ungueltig", "details": {"feld": "tag", "regel": "bereich", "max": 10}}),
    ("viessmann_vicare", _schreibgruppe_mit_sollwert,
     {"grund": "ungueltig", "details": {"feld": "schreibgruppen", "regel": "nicht_erlaubt"}}),
], ids=["tagesbudget-1000", "schreibgruppe-mit-sollwert"])
def test_ak_g6_03_lockerung_wird_abgewiesen(welt, satz, aendern, fehler):
    anlage = welt(satz, "Heizkoerper")
    anlage.starten()
    neu = konfiguration(satz, "Heizkoerper")
    aendern(neu)
    anlage.konfiguration_senden(2, neu)  # der Server lockert den Schreibschutz
    anlage.runde(2)
    antworten = [r for r in anlage.link.of("up/result") if r["dokument"] == "konfiguration" and r["version"] == 2]
    assert [(r["ok"], r["error"]) for r in antworten] == [(False, fehler)]  # abgewiesen, mit Feld und Regel
    if satz == "weishaupt_wwp":  # das alte Dokument gilt: das Tageslimit bleibt bei 10
        assert len(_fuenfzehn_werte(anlage)) <= 10
    else:  # das alte Dokument gilt: die Werte des Servers werden geschrieben
        forderung = FORDERUNG[(satz, "Heizkoerper")]
        anlage.raum(22.0)
        anlage.wunsch(21.5)
        anlage.runde(2)
        anlage.server_antwortet(forderung)
        anlage.runde(2)
        for hebel in forderung:
            ziel = schreibziel(satz, hebel)
            assert len([1 for e, _, _ in anlage.ha.schreibvorgaenge if e == ziel]) == 1, hebel
