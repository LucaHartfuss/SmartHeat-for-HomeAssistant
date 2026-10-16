"""Konformitaet der Geraete-Hosts (V2; Audit 5 A5-58; Spec Stufe 3 S6 4.1): dieselben Pruefungen fuer HaDeviceHost und
GatewayDeviceHost. Jeder Host baut eine Welt (Fixture in seiner Testdatei); die Pruefungen kennen nur die gemeinsame
Sicht: Binden des Workers, Nachstellen von Neustart und Bedienung, Ursprungswerte, Dokumentsicht, Aufraeum-Markierung,
Bedienwunsch. Rueckrufe des Geraetekerns werfen nie."""
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from smartheat_core import dateien
from smartheat_runtime import dokument_config
from smartheat_runtime.soll_eingang import EV_TARGET_INPUT
from smartheat_runtime.soll_speicher import SOURCE_PORTAL
from smartheat_runtime.worker import RegulationWorker


class KonformDevice:
    """Die Sicht des Geraetekerns, die beide Hosts nutzen."""

    def __init__(self, konfiguration: dict | None) -> None:
        self.konf = konfiguration
        self.bed: dict | None = None
        self.dirty = False
        self.wuensche: list = []
        self.aus: list = []
        self.meldungen: list = []
        self.documents = SimpleNamespace(aktuell=lambda art: SimpleNamespace(dirty=self.dirty)
                                         if art == "bedienung" and self.bed is not None else None)

    def konfiguration(self):
        return self.konf

    def bedienung(self):
        return self.bed

    def abo_status(self):
        return dokument_config.abo_status(self.konf)

    def wunsch(self, wert, *, herkunft, durch=None):
        self.wuensche.append((wert, herkunft, durch))
        return min(max(float(wert), 15.0), 25.0)

    def soll_quelle_aus(self, aus, wirksam=None):
        self.aus.append((aus, wirksam))

    def meldung(self, key, text, *, offen, kritisch=True):
        self.meldungen.append((key, text, offen, kritisch))


@dataclass
class Welt:
    host: object  # HaDeviceHost oder GatewayDeviceHost, noch nicht verbunden
    clock: Callable[[], float]
    reload_kind: str
    aufraeumen_pfad: Path
    konfiguration: dict  # eine eingerichtete Konfiguration des Hosts
    wunsch_herkunft: str  # herkunft, mit der host.wunsch(wert) den Geraetekern ruft

    def verbunden(self) -> KonformDevice:
        device = KonformDevice(dict(self.konfiguration))
        self.host.verbinden(device)
        return device

    def worker(self) -> tuple[RegulationWorker, list]:
        worker = RegulationWorker(clock=self.clock)
        seen: list = []
        worker.register(self.reload_kind, lambda event: seen.append((event.kind, None)))
        worker.register(EV_TARGET_INPUT, lambda event: seen.append((event.kind, dict(event.data))))
        return worker, seen


def ohne_geraet_gelten_feste_leerwerte(w: Welt) -> None:
    h = w.host
    assert h.konfiguration() is None
    assert h.bedienung() == (None, False)
    assert h.abo_status() == dokument_config.abo_status(None)
    assert h.wunsch(22.5) == 22.5
    h.soll_quelle_aus(True, None)  # wirft nicht


def dokumentsicht_fuer_die_laufzeit(w: Welt) -> None:
    device = w.verbunden()
    device.bed, device.dirty = {"raum_soll": 21.5}, True
    assert w.host.bedienung() == ({"raum_soll": 21.5}, True)
    assert w.host.konfiguration() == w.konfiguration
    assert w.host.abo_status() == dokument_config.abo_status(w.konfiguration)


def bedienung_vor_dem_binden_wird_nachgestellt(w: Welt) -> None:
    w.verbunden()
    w.host.bedienung_uebernommen({"raum_soll": 22})  # aus JSON kommt eine ganze Zahl als int
    worker, seen = w.worker()
    w.host.worker_binden(worker)
    worker.run_pending()
    assert seen == [(EV_TARGET_INPUT, {"value": 22.0, "source": SOURCE_PORTAL})]
    assert type(seen[0][1]["value"]) is float


def nur_die_letzte_bedienung_vor_dem_binden_zaehlt(w: Welt) -> None:
    w.verbunden()
    w.host.bedienung_uebernommen({"raum_soll": 21.0})
    w.host.bedienung_uebernommen({"raum_soll": 23.5})
    worker, seen = w.worker()
    w.host.worker_binden(worker)
    worker.run_pending()
    assert seen == [(EV_TARGET_INPUT, {"value": 23.5, "source": SOURCE_PORTAL})]


def bedienung_nach_dem_binden_geht_sofort_an_den_worker(w: Welt) -> None:
    w.verbunden()
    worker, seen = w.worker()
    w.host.worker_binden(worker)
    w.host.bedienung_uebernommen({"raum_soll": 22.5})
    worker.run_pending()
    assert seen == [(EV_TARGET_INPUT, {"value": 22.5, "source": SOURCE_PORTAL})]


def ungueltige_bedienung_wird_still_ignoriert(w: Welt) -> None:
    w.verbunden()
    for inhalt in ({}, {"raum_soll": None}, {"raum_soll": True}, {"raum_soll": "22"}, {"raum_soll": [22]}):
        w.host.bedienung_uebernommen(inhalt)  # vor dem Binden
    worker, seen = w.worker()
    w.host.worker_binden(worker)
    for inhalt in ({}, {"raum_soll": False}, {"raum_soll": {"x": 1}}):
        w.host.bedienung_uebernommen(inhalt)  # nach dem Binden
    worker.run_pending()
    assert seen == []


def im_ruhezustand_geht_keine_bedienung_an_den_worker(w: Welt) -> None:
    w.verbunden()
    w.host.bedienung_uebernommen({"raum_soll": 22.0})
    worker, seen = w.worker()
    w.host.worker_binden(worker, soll_eingang=False)
    w.host.bedienung_uebernommen({"raum_soll": 22.5})
    worker.run_pending()
    assert seen == []


def neustart_vor_dem_binden_kommt_beim_binden(w: Welt) -> None:
    w.verbunden()
    w.host.neustart_anfordern()
    worker, seen = w.worker()
    w.host.worker_binden(worker)
    worker.run_pending()
    assert seen == [(w.reload_kind, None)]


def neustart_nach_dem_binden_kommt_sofort(w: Welt) -> None:
    w.verbunden()
    worker, seen = w.worker()
    w.host.worker_binden(worker)
    w.host.neustart_anfordern()
    worker.run_pending()
    assert seen == [(w.reload_kind, None)]


def ursprungswerte_sind_kopien(w: Welt) -> None:
    werte = {"heat_limit": 18.0}
    w.host.ursprungswerte_merken(werte)
    werte["heat_limit"] = 0.0
    gelesen = w.host.ursprungswerte()
    gelesen["fremd"] = 1
    assert w.host.ursprungswerte() == {"heat_limit": 18.0}


def leer_nach_eingerichtet_markiert_aufraeumen_und_startet_neu(w: Welt) -> None:
    w.verbunden()
    alt = dict(w.konfiguration)
    w.host.konfiguration_uebernommen(alt, {**alt, "anlage": None})
    assert dateien.lesen_json(w.aufraeumen_pfad) == {"anlage": alt["anlage"], "konfiguration": alt,
                                                     "zurueckgesetzt": False}
    worker, seen = w.worker()
    w.host.worker_binden(worker)
    worker.run_pending()
    assert seen == [(w.reload_kind, None)]


def die_erste_leere_konfiguration_markiert_nichts(w: Welt) -> None:
    w.verbunden()
    w.host.konfiguration_uebernommen(None, {"anlage": None})
    assert not w.aufraeumen_pfad.exists()


def eine_unerhebliche_aenderung_startet_nicht_neu(w: Welt) -> None:
    w.verbunden()
    alt = dict(w.konfiguration)
    w.host.konfiguration_uebernommen(alt, {**alt, "zz_unerheblich": 1})  # kein Feld aus LAUFZEIT_FELDER
    worker, seen = w.worker()
    w.host.worker_binden(worker)
    worker.run_pending()
    assert seen == []


def wunsch_und_soll_quelle_gehen_an_das_geraet(w: Welt) -> None:
    device = w.verbunden()
    assert w.host.wunsch(30.0) == 25.0
    w.host.soll_quelle_aus(True, 21.0)
    assert device.wuensche == [(30.0, w.wunsch_herkunft, None)] and device.aus == [(True, 21.0)]


def host_und_version(w: Welt) -> None:
    assert w.host.host in ("ha", "gateway") and isinstance(w.host.version, str) and w.host.version


PRUEFUNGEN = (
    ohne_geraet_gelten_feste_leerwerte, dokumentsicht_fuer_die_laufzeit, bedienung_vor_dem_binden_wird_nachgestellt,
    nur_die_letzte_bedienung_vor_dem_binden_zaehlt, bedienung_nach_dem_binden_geht_sofort_an_den_worker,
    ungueltige_bedienung_wird_still_ignoriert, im_ruhezustand_geht_keine_bedienung_an_den_worker,
    neustart_vor_dem_binden_kommt_beim_binden, neustart_nach_dem_binden_kommt_sofort, ursprungswerte_sind_kopien,
    leer_nach_eingerichtet_markiert_aufraeumen_und_startet_neu, die_erste_leere_konfiguration_markiert_nichts,
    eine_unerhebliche_aenderung_startet_nicht_neu, wunsch_und_soll_quelle_gehen_an_das_geraet, host_und_version,
)


def parameter(abweichungen: dict[str, str]):
    """PRUEFUNGEN fuer pytest.mark.parametrize; bekannte Abweichungen eines Hosts als strikte xfail (verschwindet die
    Abweichung, wird der Lauf rot, und die Zeile muss raus)."""
    import pytest

    return [pytest.param(p, id=p.__name__, marks=pytest.mark.xfail(strict=True, reason=abweichungen[p.__name__]))
            if p.__name__ in abweichungen else pytest.param(p, id=p.__name__) for p in PRUEFUNGEN]
