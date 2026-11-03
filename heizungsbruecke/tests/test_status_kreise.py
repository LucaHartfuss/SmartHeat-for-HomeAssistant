"""Mehrere Kreise melden ein Ereignis der Anlage: die Felder der Anlage folgen dem schlechtesten Kreis, die Felder
des Kreises (Boost, Hebelsatz, Hebel, Gelerntes, Abo) kommen vom niedrigsten Kreis."""
from datetime import UTC, datetime

import pytest

from smartheat_core.levers import LEVER_SETS
from smartheat_runtime import status
from smartheat_runtime.delivery import DataFault, DeliveryState
from smartheat_runtime.state import BridgeState
from smartheat_runtime.status import (
    EVENT_FIELDS,
    Flags,
    Statussammler,
    build_event,
    zusammenfassen,
)

SEIT = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
VAILLANT = LEVER_SETS["vaillant_vrc720"]
WEISHAUPT = LEVER_SETS["weishaupt_wwp"]


def _ereignis(flags=None, state=None, lever_set=VAILLANT):
    return build_event("t1", flags or Flags(gestartet=True), state or BridgeState(), lever_set, "1.0")


def test_ein_kreis_bleibt_unveraendert():
    ereignis = _ereignis()
    assert zusammenfassen({1: ereignis}) == ereignis


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


def test_die_felder_des_kreises_kommen_vom_niedrigsten_kreis():
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


def test_das_ergebnis_hat_genau_die_felder_des_vertrags():
    erg = zusammenfassen({1: _ereignis(), 2: _ereignis(Flags(abo_beendet=True))})
    assert tuple(erg) == EVENT_FIELDS


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
