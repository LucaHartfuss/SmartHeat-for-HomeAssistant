"""Fakes fuer den Geraetekern in den Gateway-Tests (Plan 5b D2): Zugang von der Test-CA (fake_ca), Link ohne Netz, ein
Bootstrap, der nie laufen darf (der Zugang liegt schon vor), und eine Geraetesicht ohne Geraetekern fuer Host-Tests.
Gegenstuecke der Hilfen in heizungsbruecke/tests (D1), die hier nicht importierbar sind."""
import itertools
import json
from pathlib import Path
from types import SimpleNamespace

from fake_ca import GeraeteCa

from smartheat_device import bootstrap, identity
from smartheat_gateway.geraet_host import GatewayDeviceHost
from smartheat_runtime import dokument_config


def zugang_ablegen(device_dir: Path, ca: GeraeteCa) -> bootstrap.Zugang:
    """Identitaet anlegen und einen Zugang wie nach dem Bootstrap speichern (kein register im Test)."""
    ident = identity.load_or_create(device_dir)
    zugang = bootstrap.Zugang(ca.sign_csr(identity.csr_pem(ident)),
                              bootstrap.Endpoint("mqtt.e2e.test", 8883, ca.pem))
    bootstrap.speichern(device_dir, bootstrap.Antwort("uebernommen", zugang))
    return zugang


class KeinBootstrap:
    def register(self, **kwargs):
        raise AssertionError("Bootstrap darf in diesem Test nicht laufen")

    def certificate(self):
        raise AssertionError("Bootstrap darf in diesem Test nicht laufen")


_CLIENT_NUMMERN = itertools.count(1)


class FakeLink:
    def __init__(self, device_id, zugang, key_pem, ereignis) -> None:
        self.device_id, self.zugang, self.ereignis = device_id, zugang, ereignis
        self.connected = self.started = self.stopped = False
        self.sent: list[tuple[str, dict]] = []
        self.connect_failures = 0
        self.client_nr = 0

    def start(self):
        self.started = True
        self.client_nr = next(_CLIENT_NUMMERN)

    def stop(self):
        self.stopped, self.connected = True, False

    def publish(self, name, payload):
        if not self.connected:
            return None
        # allow_nan=False wie link.Link.publish: NaN/Inf gehen nie hinaus, json wirft ValueError (Device._publish faengt es)
        self.sent.append((name, json.loads(json.dumps(payload, allow_nan=False))))
        if name == "up/inventory":  # PUBACK des Brokers: das Ergebnis der Inventur wartet darauf (L4-02)
            self.ereignis("gesendet", len(self.sent), self.client_nr, True)
        return len(self.sent)

    def port_wechseln_falls_noetig(self):
        pass

    def verbinden(self):
        self.connected = True
        self.ereignis("verbunden")

    def trennen(self):
        self.connected = False
        self.ereignis("getrennt")

    def empfangen(self, name, body):
        self.ereignis("nachricht", name, json.dumps(body).encode())

    def of(self, name):
        return [payload for topic, payload in self.sent if topic == name]


class FakeDeviceSicht:
    """Was GatewayDeviceHost vom Geraetekern liest, ohne Geraetekern (Host-Tests)."""

    def __init__(self, konf: dict | None, bedienung: dict | None = None, dirty: bool = False) -> None:
        self.konf, self.bed, self.dirty = konf, bedienung, dirty
        self.identity = SimpleNamespace(device_id="shg-aaaaaaaaaaaaaaaa", claim_code="ABCDEFGHIJKL")
        self.documents = SimpleNamespace(
            aktuell=lambda art: SimpleNamespace(dirty=self.dirty) if self.bed is not None else None)
        self.wuensche: list = []
        self.stopped = False

    def konfiguration(self):
        return self.konf

    def bedienung(self):
        return self.bed

    def abo_status(self):
        return dokument_config.abo_status(self.konf)

    def wunsch(self, wert, *, herkunft, durch=None):
        self.wuensche.append(wert)
        return wert

    def soll_quelle_aus(self, aus, wirksam=None):
        pass

    def meldung(self, key, text, *, offen, kritisch=True):
        pass

    def stop(self):
        self.stopped = True


def geraet_fuer(paths, mirror, konf, bedienung=None, *, dirty=False, clock=None) -> GatewayDeviceHost:
    geraet = GatewayDeviceHost(paths, mirror, host_dir=paths.data.parent / "host",
                               **({"clock": clock} if clock is not None else {}))
    geraet.verbinden(FakeDeviceSicht(konf, bedienung, dirty))
    return geraet
