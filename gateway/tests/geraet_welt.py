"""Gemeinsame Testhilfe: `Welt` ist ein `GatewayDeviceHost` auf einem Datenordner mit Spiegel, Attrappen-Geraet und
Worker (test_geraet_host.py, host/tests/test_kanal.py)."""
from types import SimpleNamespace

from configs import konfiguration
from fakes import FakeBus

from smartheat_device.geraet_host_basis import EV_RELOAD
from smartheat_gateway import geraet_host as gh
from smartheat_gateway.paths import Paths
from smartheat_gateway.triggers import EV_TARGET_INPUT
from smartheat_gateway.zigbee import ZigbeeMirror
from smartheat_runtime import dokument_config
from smartheat_runtime.worker import RegulationWorker

WALL = 1_800_000_000.0


class FakeDevice:
    def __init__(self) -> None:
        self.konf: dict | None = konfiguration()
        self.bed: dict | None = None
        self.dirty = False
        self.meldungen, self.ergebnisse, self.wuensche, self.aus = [], [], [], []
        self.ergebnis_ok = True
        self.identity = SimpleNamespace(device_id="shg-aaaaaaaaaaaaaaaa", claim_code="ABCDEFGHIJKL")
        self.documents = SimpleNamespace(
            aktuell=lambda art: SimpleNamespace(dirty=self.dirty) if self.bed is not None else None)

    def konfiguration(self):
        return self.konf

    def bedienung(self):
        return self.bed

    def abo_status(self):
        return dokument_config.abo_status(self.konf)

    def wunsch(self, wert, *, herkunft, durch=None):
        self.wuensche.append((wert, herkunft, durch))
        return max(15.0, min(25.0, wert))

    def soll_quelle_aus(self, aus, wirksam=None):
        self.aus.append((aus, wirksam))

    def meldung(self, key, text, *, offen, kritisch=True):
        self.meldungen.append((key, text, offen, kritisch))

    def ergebnis_software(self, version, ok, grund=None):
        self.ergebnisse.append((version, ok, grund))
        return self.ergebnis_ok


class Welt:
    def __init__(self, tmp_path, clock) -> None:
        self.paths = Paths(tmp_path / "data")
        self.host_dir = tmp_path / "host"
        self.clock = clock
        self.wall = {"t": WALL}
        self.mirror = ZigbeeMirror(FakeBus(), clock)
        self.host = gh.GatewayDeviceHost(self.paths, self.mirror, host_dir=self.host_dir, clock=clock,
                                         wall=lambda: self.wall["t"])
        self.device = FakeDevice()
        self.host.verbinden(self.device)
        self.worker = RegulationWorker(clock=clock)
        self.events: list = []
        self.worker.register(EV_RELOAD, self.events.append)
        self.worker.register(EV_TARGET_INPUT, self.events.append)

    def kinds(self):
        self.worker.run_pending()
        return [event.kind for event in self.events]
