"""Gemeinsamer Teil der Geraete-Hosts (Spec Stufe 3 S6 4.2); das Verhalten beider Hosts prueft
test_host_konformitaet.py, hier nur, was ohne Host gilt."""
import threading
from pathlib import Path

import pytest
from conftest import FakeClock

from smartheat_device.geraet_host_basis import EV_RELOAD, GeraetHostBasis
from smartheat_runtime.worker import RegulationWorker


class _Host(GeraetHostBasis):
    host, version, wunsch_herkunft = "test", "0.0.0", "test"

    def __init__(self, pfad: Path) -> None:
        super().__init__()
        self._pfad = pfad

    def aufraeumen_pfad(self) -> Path:
        return self._pfad


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
