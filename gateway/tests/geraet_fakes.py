"""Fakes fuer den Geraetekern in den Gateway-Tests (Plan 5b D2): Test-CA, Link ohne Netz, ein Bootstrap, der nie
laufen darf (der Zugang liegt schon vor), und eine Geraetesicht ohne Geraetekern fuer Host-Tests. Gegenstuecke der
Hilfen in heizungsbruecke/tests (D1), die hier nicht importierbar sind."""
import datetime
import json
from pathlib import Path
from types import SimpleNamespace

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from smartheat_device import bootstrap, identity
from smartheat_gateway.geraet_host import GatewayDeviceHost
from smartheat_runtime import dokument_config


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class GeraeteCa:
    def __init__(self) -> None:
        self._key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "smartheat-test-ca")])
        self._cert = (
            x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(self._key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(_now() - datetime.timedelta(minutes=1))
            .not_valid_after(_now() + datetime.timedelta(days=2))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(self._key, hashes.SHA256()))
        self.pem = self._cert.public_bytes(serialization.Encoding.PEM).decode()

    def sign_csr(self, csr_pem: str) -> str:
        csr = x509.load_pem_x509_csr(csr_pem.encode())
        cert = (
            x509.CertificateBuilder().subject_name(csr.subject).issuer_name(self._cert.subject)
            .public_key(csr.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(_now() - datetime.timedelta(minutes=1)).not_valid_after(_now() + datetime.timedelta(days=2))
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
            .sign(self._key, hashes.SHA256()))
        return cert.public_bytes(serialization.Encoding.PEM).decode()


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


class FakeLink:
    def __init__(self, device_id, zugang, key_pem, ereignis) -> None:
        self.device_id, self.zugang, self.ereignis = device_id, zugang, ereignis
        self.connected = self.started = self.stopped = False
        self.sent: list[tuple[str, dict]] = []
        self.connect_failures = 0

    def start(self):
        self.started = True

    def stop(self):
        self.stopped, self.connected = True, False

    def publish(self, name, payload):
        if not self.connected:
            return None
        self.sent.append((name, json.loads(json.dumps(payload))))
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
