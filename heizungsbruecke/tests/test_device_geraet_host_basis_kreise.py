"""Gemeinsamer Teil der Geraete-Hosts bei mehreren Kreisen: Worker, Bedienung und Wunsch je Kreis, Neustart,
Konfigurationsaenderung ueber die Laufzeit."""
import threading
import time
from pathlib import Path

from fakes import FakeClock
from geraet_helpers import Device, gateway_konfiguration

from smartheat_device.geraet_host_basis import EV_RELOAD, GeraetHostBasis
from smartheat_runtime.soll_eingang import EV_TARGET_INPUT
from smartheat_runtime.worker import RegulationWorker


class _Host(GeraetHostBasis):
    host, version, wunsch_herkunft = "test", "0.0.0", "test"

    def __init__(self, pfad: Path) -> None:
        super().__init__()
        self._pfad = pfad

    def aufraeumen_pfad(self) -> Path:
        return self._pfad

    def abgemeldet_pfad(self) -> Path:
        return self._pfad.with_name("abgemeldet.json")


def _bedienung(*soll) -> dict:
    return {"kreise": [{"kreis": nr, "modus": "manuell", "raum_soll": wert} for nr, wert in enumerate(soll, 1)]}


def _worker(clock, host, kreis, **optionen):
    worker = RegulationWorker(clock)
    gesehen: list = []
    worker.register(EV_TARGET_INPUT, lambda event: gesehen.append(event.data["value"]))
    worker.register(EV_RELOAD, lambda event: gesehen.append("reload"))
    host.worker_binden(worker, kreis=kreis, **optionen)
    return worker, gesehen


def test_eine_bedienung_erreicht_nur_den_worker_ihres_kreises(tmp_path):
    host, clock = _Host(tmp_path / "a.json"), FakeClock()
    eins, bei_eins = _worker(clock, host, 1)
    zwei, bei_zwei = _worker(clock, host, 2)
    host.bedienung_uebernommen(_bedienung(21.0, 23.5))
    host.bedienung_uebernommen({"kreise": [{"kreis": 2, "modus": "manuell", "raum_soll": 19.0}]})  # Kreis 1 nicht genannt
    eins.run_pending()
    zwei.run_pending()
    assert bei_eins == [21.0] and bei_zwei == [23.5, 19.0]


def test_die_letzte_bedienung_vor_dem_binden_wird_je_kreis_nachgestellt(tmp_path):
    host, clock = _Host(tmp_path / "a.json"), FakeClock()
    host.bedienung_uebernommen(_bedienung(21.0, 23.5))
    host.bedienung_uebernommen(_bedienung(22.0, 24.0))
    zwei, bei_zwei = _worker(clock, host, 2)
    zwei.run_pending()
    assert bei_zwei == [24.0]  # Kreis 1 noch ungebunden: seine Bedienung wartet
    eins, bei_eins = _worker(clock, host, 1)
    eins.run_pending()
    assert bei_eins == [22.0]


def test_ein_kreis_im_ruhezustand_nimmt_keine_bedienung_an_der_andere_schon(tmp_path):
    host, clock = _Host(tmp_path / "a.json"), FakeClock()
    eins, bei_eins = _worker(clock, host, 1, soll_eingang=False)
    zwei, bei_zwei = _worker(clock, host, 2)
    host.bedienung_uebernommen(_bedienung(21.0, 23.5))
    eins.run_pending()
    zwei.run_pending()
    assert bei_eins == [] and bei_zwei == [23.5]


def test_ein_neu_gebundener_worker_ersetzt_den_alten_des_kreises(tmp_path):
    host, clock = _Host(tmp_path / "a.json"), FakeClock()
    alt, bei_alt = _worker(clock, host, 2)
    neu, bei_neu = _worker(clock, host, 2)
    host.bedienung_uebernommen(_bedienung(21.0, 23.5))
    alt.run_pending()
    neu.run_pending()
    assert bei_alt == [] and bei_neu == [23.5]


def test_ein_neustart_erreicht_jeden_gebundenen_worker_und_den_spaeter_gebundenen(tmp_path):
    host, clock = _Host(tmp_path / "a.json"), FakeClock()
    eins, bei_eins = _worker(clock, host, 1)
    zwei, bei_zwei = _worker(clock, host, 2)
    host.neustart_anfordern()
    eins.run_pending()
    zwei.run_pending()
    assert bei_eins == ["reload"] and bei_zwei == ["reload"]
    drei, bei_drei = _worker(clock, host, 3)
    drei.run_pending()
    assert bei_drei == ["reload"]


def test_der_wunsch_traegt_die_nummer_seines_kreises(tmp_path):
    host = _Host(tmp_path / "a.json")
    host.verbinden(device := Device())
    host.wunsch(21.0)
    host.wunsch(22.0, kreis=2)
    assert device.wunsch_kreise == [1, 2]
    assert [wert for wert, *_ in device.wuensche] == [21.0, 22.0]


class _Laufzeit:
    def __init__(self, antwort=False, sofort=False) -> None:
        self.antwort, self.sofort, self.aufrufe, self.thread = antwort, sofort, [], None

    def prozessneustart_noetig(self, alt, neu):
        if isinstance(self.sofort, Exception):
            raise self.sofort
        return self.sofort

    def konfiguration_geaendert(self, alt, neu):
        self.thread = threading.current_thread()
        self.aufrufe.append((alt, neu))
        if isinstance(self.antwort, Exception):
            raise self.antwort
        return self.antwort


def _warten(bedingung, schritte=300):
    for _ in range(schritte):
        if bedingung():
            return True
        time.sleep(0.01)
    return False


def test_eine_konfiguration_geht_an_die_laufzeit_und_ohne_forderung_gibt_es_keinen_neustart(tmp_path):
    host, clock = _Host(tmp_path / "a.json"), FakeClock()
    laufzeit = _Laufzeit(antwort=False)
    host.laufzeit_binden(laufzeit)
    eins, bei_eins = _worker(clock, host, 1)
    alt, neu = _eingerichtet(version=1), _eingerichtet(version=2)
    host.konfiguration_uebernommen(alt, neu)
    assert _warten(lambda: laufzeit.aufrufe == [(alt, neu)])
    assert laufzeit.thread is not threading.current_thread()  # blockierende Wiederaufnahme nie im Geraete-Thread
    time.sleep(0.05)
    eins.run_pending()
    assert not host._neustart and bei_eins == []


def test_verlangt_die_laufzeit_den_prozessneustart_geht_er_an_alle_worker(tmp_path):
    host, clock = _Host(tmp_path / "a.json"), FakeClock()
    host.laufzeit_binden(_Laufzeit(antwort=True))
    eins, bei_eins = _worker(clock, host, 1)
    zwei, bei_zwei = _worker(clock, host, 2)
    host.konfiguration_uebernommen(_eingerichtet(version=1), _eingerichtet(version=2))
    assert _warten(lambda: host._neustart)
    eins.run_pending()
    zwei.run_pending()
    assert bei_eins == ["reload"] and bei_zwei == ["reload"]


def test_der_prozessneustart_aus_der_vorpruefung_wird_sofort_angefordert(tmp_path):
    host = _Host(tmp_path / "a.json")
    laufzeit = _Laufzeit(antwort=False, sofort=True)
    host.laufzeit_binden(laufzeit)
    host.konfiguration_uebernommen(_eingerichtet(version=1), _eingerichtet(version=2))
    assert host._neustart  # ohne Warten auf den Hintergrund: der Ablauf des Kreises sieht ihn gleich
    time.sleep(0.05)
    assert laufzeit.aufrufe == []


def test_scheitert_die_vorpruefung_wird_der_prozess_sofort_neu_gestartet(tmp_path):
    host = _Host(tmp_path / "a.json")
    host.laufzeit_binden(_Laufzeit(sofort=RuntimeError("kaputt")))
    host.konfiguration_uebernommen(_eingerichtet(version=1), _eingerichtet(version=2))
    assert host._neustart


def test_scheitert_die_laufzeit_bei_der_aenderung_wird_der_prozess_neu_gestartet(tmp_path):
    host = _Host(tmp_path / "a.json")
    host.laufzeit_binden(_Laufzeit(antwort=RuntimeError("kaputt")))
    host.konfiguration_uebernommen(_eingerichtet(version=1), _eingerichtet(version=2))
    assert _warten(lambda: host._neustart)


def test_ohne_eingerichtete_konfiguration_fragt_der_host_die_laufzeit_nicht(tmp_path):
    host = _Host(tmp_path / "a.json")
    laufzeit = _Laufzeit(antwort=True)
    host.laufzeit_binden(laufzeit)
    host.konfiguration_uebernommen({"anlage": None}, {"anlage": None})
    time.sleep(0.1)
    assert laufzeit.aufrufe == [] and not host._neustart


def _eingerichtet(version):
    """Eine eingerichtete Gateway-Konfiguration; `version` unterscheidet zwei Staende (Tagestick)."""
    return gateway_konfiguration(tagestick=f"12:{version:02d}")


def test_eine_bedienung_mit_unbrauchbaren_eintraegen_beruehrt_nur_die_brauchbaren(tmp_path):
    host, clock = _Host(tmp_path / "a.json"), FakeClock()
    eins, bei_eins = _worker(clock, host, 1)
    host.bedienung_uebernommen({"kreise": ["kaputt", {"modus": "manuell"}, {"kreis": 1, "modus": "manuell", "raum_soll": 21.0}]})
    eins.run_pending()
    assert bei_eins == [21.0]


def test_ein_worker_ohne_angabe_zum_soll_eingang_nimmt_die_bedienung_an(tmp_path):
    host, clock = _Host(tmp_path / "a.json"), FakeClock()
    worker = RegulationWorker(clock)
    gesehen: list = []
    worker.register(EV_TARGET_INPUT, lambda event: gesehen.append(event.data["value"]))
    host.worker_binden(worker, kreis=1)
    host._soll_eingaenge.pop(1, None)  # keine Angabe zu diesem Kreis
    host.bedienung_uebernommen(_bedienung(21.0))
    worker.run_pending()
    assert gesehen == [21.0]
