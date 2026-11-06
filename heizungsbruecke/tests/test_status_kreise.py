"""Mehrere Kreise melden ein Ereignis der Anlage: die Felder der Anlage folgen dem schlechtesten Kreis, die Felder
des Kreises (Boost, Hebelsatz, Hebel, Gelerntes, Abo) stehen je Kreis in `kreise`; die flachen Felder des
Laufzeit-Ereignisses (Server, up/status) kommen vom niedrigsten Kreis."""
from datetime import UTC, datetime

import pytest

from smartheat_core.levers import LEVER_SETS
from smartheat_runtime import status
from smartheat_runtime.delivery import DataFault, DeliveryState
from smartheat_runtime.state import BridgeState
from smartheat_runtime.status import (
    ANLAGE_FELDER,
    EVENT_FIELDS,
    KREIS_FELDER,
    Flags,
    Statussammler,
    build_event,
    ohne_kreise,
    zusammenfassen,
)

SEIT = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
VAILLANT = LEVER_SETS["vaillant_vrc720"]
WEISHAUPT = LEVER_SETS["weishaupt_wwp"]


def _ereignis(flags=None, state=None, lever_set=VAILLANT):
    return build_event("t1", flags or Flags(gestartet=True), state or BridgeState(), lever_set, "1.0")


def test_ein_kreis_bleibt_bis_auf_die_kreisliste_unveraendert():
    ereignis = _ereignis()
    erg = zusammenfassen({1: ereignis})
    assert erg == {**ereignis, "kreise": [{"kreis": 1, **{feld: ereignis[feld] for feld in KREIS_FELDER}}]}
    assert ohne_kreise(erg) == ereignis


def test_anlage_und_kreis_teilen_die_felder_des_ereignisses_vollstaendig_auf():
    assert set(ANLAGE_FELDER) | set(KREIS_FELDER) == set(EVENT_FIELDS)
    assert not set(ANLAGE_FELDER) & set(KREIS_FELDER)


def test_ohne_kreis_gibt_es_kein_ereignis():
    with pytest.raises(ValueError):
        zusammenfassen({})


def test_die_felder_der_anlage_folgen_dem_schlechtesten_kreis():
    eins = _ereignis(Flags(abo_beendet=True, grund="Frist abgelaufen"))
    zwei = _ereignis(Flags(gestartet=True))
    erg = zusammenfassen({1: eins, 2: zwei})
    assert erg["status"] == "abo_beendet"
    assert erg["grund"] == "Frist abgelaufen"

    erg = zusammenfassen({1: zwei, 2: eins})
    assert erg["status"] == "abo_beendet"  # der schlechteste Kreis ist der zweite
    assert erg["grund"] == "Frist abgelaufen"


@pytest.mark.parametrize("schlechter,besser", [
    ("abgemeldet", "konfigurationsfehler"),
    ("konfigurationsfehler", "abo_beendet"),
    ("abo_beendet", "abo_inaktiv"),
    ("abo_inaktiv", "notbetrieb"),
    ("notbetrieb", "datenfehler"),
    ("datenfehler", "startet"),
    ("startet", "regelt"),
])
def test_die_reihenfolge_der_schwere_folgt_dem_gesamtzustand(schlechter, besser):
    assert status.SCHWERE.index(schlechter) < status.SCHWERE.index(besser)


def test_die_schwere_kennt_jeden_status_des_vertrags():
    assert set(status.SCHWERE) == set(status.STATUS_VALUES)


def test_notbetrieb_eines_kreises_steht_in_der_anlage_auch_wenn_ein_anderer_schlechter_ist():
    eins = _ereignis(Flags(abo_beendet=True))
    zwei = _ereignis(Flags(gestartet=True), BridgeState(delivery=DeliveryState(notbetrieb=True)))
    erg = zusammenfassen({1: eins, 2: zwei})
    assert erg["status"] == "abo_beendet"
    assert erg["notbetrieb"] is True


def test_datenfehler_und_serverantwort_folgen_dem_schlechtesten_kreis():
    eins = _ereignis(Flags(gestartet=True), BridgeState(last_ack_at="2026-10-01T08:00:00+00:00"))
    zwei = _ereignis(Flags(gestartet=True), BridgeState(
        last_ack_at="2026-10-02T08:00:00+00:00",
        delivery=DeliveryState(datenfehler=DataFault("server", ("x",)))))
    erg = zusammenfassen({1: eins, 2: zwei})
    assert erg["status"] == "datenfehler"
    assert erg["datenfehler"] == zwei["datenfehler"]
    assert erg["letzte_serverantwort"] == "2026-10-02T08:00:00+00:00"


def test_die_flachen_felder_des_laufzeit_ereignisses_kommen_vom_niedrigsten_kreis():
    eins = _ereignis(Flags(gestartet=True), BridgeState(boost_active=True, learned={"curve": 0.9}), VAILLANT)
    zwei = _ereignis(Flags(gestartet=True, abo="aktiv"), BridgeState(abo_inactive_since=SEIT), WEISHAUPT)
    erg = zusammenfassen({2: zwei, 1: eins})
    assert erg["boost"] == "komfort"
    assert erg["hebelsatz"] == VAILLANT.id
    assert erg["hebel"] == eins["hebel"]
    assert erg["gelernt"] == eins["gelernt"]
    assert erg["abo"] == eins["abo"]
    assert erg["abo_frist_ende"] == eins["abo_frist_ende"]
    assert erg["status"] == "abo_inaktiv"  # die Anlage zeigt den schlechtesten Kreis


def test_zustand_und_kaelteschutz_stehen_je_kreis_unabhaengig_in_kreise():
    eins = build_event("t1", Flags(gestartet=True), BridgeState(kaelteschutz_art="soll"), VAILLANT, "1.0",
                       kreiszustand="testphase_ohne")
    zwei = build_event("t1", Flags(gestartet=True), BridgeState(), VAILLANT, "1.0", kreiszustand="ausser_betrieb")
    erg = zusammenfassen({2: zwei, 1: eins})
    assert [(k["kreis"], k["zustand"], k["kaelteschutz"]) for k in erg["kreise"]] == [
        (1, "testphase", "soll"), (2, "ausser_betrieb", "aus")]
    assert (erg["zustand"], erg["kaelteschutz"]) == ("testphase", "soll")  # flach: der niedrigste Kreis
    assert ohne_kreise(erg)["zustand"] == "testphase"


def test_ein_abgemeldeter_kreis_macht_die_anlage_nicht_abgemeldet_solange_andere_regeln():
    """Abgemeldet ist die Anlage erst, wenn jeder Kreis es ist: sonst hielte der Server (und sign_off) sie fuer
    zurueckgesetzt, obwohl ein Kreis noch regelt."""
    abgemeldet = _ereignis(Flags(abgemeldet=True))
    regelt = _ereignis(Flags(gestartet=True))
    assert zusammenfassen({1: abgemeldet, 2: regelt})["status"] == "regelt"
    assert zusammenfassen({1: regelt, 2: abgemeldet})["status"] == "regelt"
    assert zusammenfassen({1: abgemeldet, 2: abgemeldet})["status"] == "abgemeldet"  # Grenze: alle Kreise


def test_der_schlechteste_der_uebrigen_kreise_zaehlt_neben_einem_abgemeldeten():
    abgemeldet = _ereignis(Flags(abgemeldet=True))
    fehler = _ereignis(Flags(konfigurationsfehler=True, grund="Hebelsatz"))
    erg = zusammenfassen({1: abgemeldet, 2: fehler})
    assert erg["status"] == "konfigurationsfehler"
    assert erg["grund"] == "Hebelsatz"


def test_abgemeldet_mit_grund_bleibt_im_ereignis_der_anlage_wenn_alle_abgemeldet_sind():
    """Ein Kreis, dessen Zuruecksetzen scheiterte (grund), darf die Anlage nicht als zurueckgesetzt zeigen, auch wenn
    ein niedrigerer Kreis ohne Grund abgemeldet ist."""
    sauber = _ereignis(Flags(abgemeldet=True))
    offen = _ereignis(Flags(abgemeldet=True, grund="Zuruecksetzen gescheitert"))
    assert zusammenfassen({1: sauber, 2: offen})["grund"] == "Zuruecksetzen gescheitert"
    assert zusammenfassen({1: offen, 2: sauber})["grund"] == "Zuruecksetzen gescheitert"
    assert zusammenfassen({1: sauber, 2: sauber})["grund"] is None


def test_das_ergebnis_hat_genau_die_felder_des_vertrags():
    erg = zusammenfassen({1: _ereignis(), 2: _ereignis(Flags(abo_beendet=True))})
    assert tuple(erg) == (*EVENT_FIELDS, "kreise")
    assert tuple(ohne_kreise(erg)) == EVENT_FIELDS


def test_die_kreisliste_fuehrt_jeden_kreis_aufsteigend_mit_seinen_eigenen_feldern():
    eins = _ereignis(Flags(gestartet=True), BridgeState(boost_active=True, learned={"curve": 0.9}), VAILLANT)
    zwei = _ereignis(Flags(abo_beendet=True), BridgeState(), WEISHAUPT)
    erg = zusammenfassen({2: zwei, 1: eins})
    assert [kreis["kreis"] for kreis in erg["kreise"]] == [1, 2]
    for kreis, ereignis in zip(erg["kreise"], (eins, zwei), strict=True):
        assert tuple(kreis) == ("kreis", *KREIS_FELDER)
        assert {feld: kreis[feld] for feld in KREIS_FELDER} == {feld: ereignis[feld] for feld in KREIS_FELDER}
    assert (erg["kreise"][0]["boost"], erg["kreise"][1]["boost"]) == ("komfort", "keiner")
    assert (erg["kreise"][0]["hebelsatz"], erg["kreise"][1]["hebelsatz"]) == (VAILLANT.id, WEISHAUPT.id)


def test_ein_kreis_beendet_der_andere_aktiv_stehen_getrennt_in_der_kreisliste():
    beendet = _ereignis(Flags(abo_beendet=True, grund="Frist abgelaufen"))
    aktiv = _ereignis(Flags(gestartet=True, abo="aktiv"))
    erg = zusammenfassen({1: aktiv, 2: beendet})
    assert [kreis["abo"] for kreis in erg["kreise"]] == ["aktiv", "beendet"]
    assert erg["status"] == "abo_beendet"  # die Anlage zeigt den schlechtesten Kreis


class Senke:
    def __init__(self):
        self.ereignisse = []

    def publish(self, event):
        self.ereignisse.append(event)


def test_der_sammler_sendet_nach_jedem_kreis_ein_ereignis_der_anlage():
    senke = Senke()
    sammler = Statussammler(senke)
    eins, zwei = sammler.senke(1), sammler.senke(2)

    eins.publish(_ereignis())
    assert [e["status"] for e in senke.ereignisse] == ["regelt"]

    zwei.publish(_ereignis(Flags(konfigurationsfehler=True, grund="kaputt")))
    assert [e["status"] for e in senke.ereignisse] == ["regelt", "konfigurationsfehler"]
    assert senke.ereignisse[-1]["grund"] == "kaputt"

    zwei.publish(_ereignis())  # der zweite Kreis erholt sich: die Anlage meldet wieder regelt
    assert senke.ereignisse[-1]["status"] == "regelt"


def test_ein_fehler_der_senke_erreicht_den_kreis():
    class Kaputt:
        def publish(self, event):
            raise OSError("weg")

    kreis = Statussammler(Kaputt()).senke(1)
    with pytest.raises(OSError):
        kreis.publish(_ereignis())  # der StatusReporter des Kreises faengt ihn und sendet spaeter erneut
