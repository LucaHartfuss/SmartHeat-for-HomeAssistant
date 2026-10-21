"""Gemeinsamer Teil der Geraete-Hosts (Spec Stufe 3 S6 4.2); das Verhalten beider Hosts prueft
test_host_konformitaet.py, hier nur, was ohne Host gilt."""
import threading
from pathlib import Path

import pytest
from conftest import FakeClock

from smartheat_device.geraet_host_basis import EV_RELOAD, GeraetHostBasis
from smartheat_runtime import dokument_config
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


def test_ein_neustart_aus_dem_geraetethread_waehrend_des_bindens_geht_nie_verloren(tmp_path):
    """Hauptthread bindet, Geraete-Thread fordert gleichzeitig an: genau ein EV_RELOAD erreicht den Worker."""
    for _ in range(200):
        host = _Host(tmp_path / "a.json")
        worker = RegulationWorker(FakeClock())
        kinds = []
        worker.register(EV_RELOAD, lambda event, ziel=kinds: ziel.append(event.kind))
        anfordern = threading.Thread(target=host.neustart_anfordern)
        anfordern.start()
        host.worker_binden(worker)
        anfordern.join()
        worker.run_pending()
        assert kinds == [EV_RELOAD]  # die Sperre serialisiert: entweder nachgestellt oder direkt, nie beides, nie keins


def test_der_neustart_ereignisname_ist_hostneutral():
    assert EV_RELOAD == "geraet_reload"


def test_ohne_aufraeumen_pfad_ist_die_basis_nicht_nutzbar(tmp_path):
    basis = GeraetHostBasis()
    with pytest.raises(NotImplementedError):
        basis.aufraeumen_pfad()


EINGERICHTET = {"anlage": "t1", "hebelsatz": "vaillant_vrc720", "raumfuehler": ["sensor.a"], "abo": "aktiv"}
LEER = {"anlage": None, "raumfuehler": []}  # wie der Server sie nach dem Erstkontakt schickt: raumfuehler [] != fehlend


def _angefordert(host: _Host) -> bool:
    worker = RegulationWorker(FakeClock())
    kinds = []
    worker.register(EV_RELOAD, lambda event: kinds.append(event.kind))
    host.worker_binden(worker)
    worker.run_pending()
    return kinds == [EV_RELOAD]


def test_leere_konfiguration_nach_keiner_startet_nicht_neu(tmp_path):
    """L4-02 (c): im Ruhezustand "nicht eingerichtet" liest die Laufzeit kein Laufzeitfeld; ein Neustart risse den
    Befehl mit, der gleich nach dem Erstkontakt laeuft. Auch leer nach leer mit anderem Laufzeitfeld nicht."""
    assert dokument_config.laufzeit_relevant(None, LEER)  # ohne (c) ginge der Neustart hinaus
    host = _Host(tmp_path / "a.json")
    host.konfiguration_uebernommen(None, LEER)
    host.konfiguration_uebernommen(LEER, {**LEER, "abo": "inaktiv", "tagestick": "12:00"})
    assert not _angefordert(host)


@pytest.mark.parametrize("alt, neu", [(None, EINGERICHTET), (LEER, EINGERICHTET), (EINGERICHTET, LEER),
                                     (EINGERICHTET, {**EINGERICHTET, "abo": "inaktiv"})])
def test_eine_eingerichtete_konfiguration_startet_neu(tmp_path, alt, neu):
    """Gegenprobe zu (c): ist die alte oder die neue Konfiguration eingerichtet, bleibt der Neustart."""
    host = _Host(tmp_path / "a.json")
    host.konfiguration_uebernommen(alt, neu)
    assert _angefordert(host)


def test_leer_nach_leer_hebt_die_abmeldung_nicht_auf_und_startet_nicht_neu(tmp_path):
    """Die Abmelde-Markierung faellt nur durch eine eingerichtete Konfiguration derselben Anlage (dann Neustart, auch
    ohne Laufzeitaenderung); (c) aendert daran nichts."""
    host = _Host(tmp_path / "a.json")
    host.abgemeldet_pfad().write_text('{"anlage": "t1"}')
    host.konfiguration_uebernommen(None, LEER)
    assert not _angefordert(host) and host.abgemeldet_pfad().exists()
    host.konfiguration_uebernommen(EINGERICHTET, dict(EINGERICHTET))  # dieselbe Anlage, nichts Laufzeitrelevantes
    assert _angefordert(host) and not host.abgemeldet_pfad().exists()
