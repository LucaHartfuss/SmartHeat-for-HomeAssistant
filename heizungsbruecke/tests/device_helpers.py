"""Hilfen fuer die Tests des Geraetekerns (Plan 5b D1): Test-CA, die CSRs des Geraets signiert (wie die lokale Test-CA
des Servers, S1 Task 6), dazu die Attrappen und Eingaben, die mehrere Testdateien teilen (Fixrunde B TP4, A6-90; vorher
in test_device_device.py, test_device_bootstrap.py, test_device_commands.py und test_device_documents.py): die Welt des
Geraets (World), Bootstrap-Antworten, Befehle, Dokumente. Plain-Import (`from device_helpers import ...`), tests/ hat
kein __init__.py."""
import datetime
import itertools
import json

from bindung_beispiele import parameter
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from smartheat_device import bootstrap, device, identity
from smartheat_device import documents as docs


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class GeraeteCa:
    """Selbstsignierte EC-CA; sign_csr stellt ein Client-Zertifikat fuer den Schluessel und CN des CSR aus."""

    def __init__(self, cn: str = "smartheat-test-ca") -> None:
        self._key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
        self._cert = (
            x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(self._key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(_now() - datetime.timedelta(minutes=1))
            .not_valid_after(_now() + datetime.timedelta(days=2))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=False, content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False,
            ), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(self._key.public_key()), critical=False)
            .sign(self._key, hashes.SHA256())
        )
        self.pem = self._cert.public_bytes(serialization.Encoding.PEM).decode()

    def sign_csr(self, csr_pem: str) -> str:
        csr = x509.load_pem_x509_csr(csr_pem.encode())
        cert = (
            x509.CertificateBuilder().subject_name(csr.subject).issuer_name(self._cert.subject)
            .public_key(csr.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(_now() - datetime.timedelta(minutes=1))
            .not_valid_after(_now() + datetime.timedelta(days=2))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self._key.public_key()), critical=False)
            .sign(self._key, hashes.SHA256())
        )
        return cert.public_bytes(serialization.Encoding.PEM).decode()


def zertifikat_fuer(ident: identity.Identity, ca: GeraeteCa) -> str:
    return ca.sign_csr(identity.csr_pem(ident))


def csr_passt(csr_pem: str, ident: identity.Identity) -> bool:
    """Gehoert der CSR zum Geraet? CN = Geraete-ID, gueltige Signatur, oeffentlicher Schluessel = TLS-Schluessel.
    (ECDSA ist randomisiert: zwei csr_pem-Aufrufe sind nie byte-gleich, daher kein PEM-Vergleich.)"""
    csr = x509.load_pem_x509_csr(csr_pem.encode())
    cn = csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    return (cn == ident.device_id and csr.is_signature_valid
            and csr.public_key().public_numbers() == ident.tls_key.public_key().public_numbers())


WALL = 1_800_000_000.0
SENSOR = "zigbee:0x00124b0000000001:temperature"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeHost:
    host = "gateway"

    def __init__(self, version="0.6.0") -> None:
        self.version = version
        self.synchron: bool | None = True
        self.konfigurationen, self.bedienungen, self.software, self.anzeigen_liste = [], [], [], []
        self.status: dict | None = {"status": "regelt"}
        self.raum_wert: dict | None = {"ist": 20.0, "soll": 21.0, "soll_quelle": "portal", "ts": "t0"}
        self.treiber_details: dict | None = None
        self.handlers: dict = {}
        self.ursprung: dict = {}

    def ursprungswerte(self):
        return dict(self.ursprung)

    def faehigkeiten(self):
        return {"drivers": ["simulation"], "zigbee": True, "updater": True}

    def uhr_synchron(self):
        return self.synchron

    def treiber_pruefen(self, bindung):
        return self.treiber_details

    def befehle_eintragen(self, register):
        for kind, handler in self.handlers.items():
            register.register(kind, handler)

    def konfiguration_vorbereiten(self, alt, neu):
        pass

    def konfiguration_uebernommen(self, alt, neu):
        self.konfigurationen.append((alt, neu))

    def bedienung_uebernommen(self, inhalt):
        self.bedienungen.append(inhalt)

    def software_soll(self, software):
        self.software.append(software)

    def runtime_status(self):
        return self.status

    def raum(self):
        return self.raum_wert

    def anzeigen(self, anzeige):
        self.anzeigen_liste.append(anzeige)


_CLIENT_NUMMERN = itertools.count(1)


class FakeLink:
    def __init__(self, device_id, zugang, key_pem, ereignis) -> None:
        self.device_id, self.zugang, self.key_pem, self.ereignis = device_id, zugang, key_pem, ereignis
        self.connected = self.started = self.stopped = False
        self.sent: list[tuple[str, dict]] = []
        self.connect_failures = 0
        self.client_nr = 0
        self._mid_basis = 0  # paho zaehlt mids je Client ab 1

    def start(self):
        self.started = True
        self.client_nr = next(_CLIENT_NUMMERN)

    def stop(self):
        self.stopped, self.connected = True, False

    def publish(self, name, payload):
        if not self.connected:
            return None
        # wie der echte Link: nie NaN/Inf hinaus (Testoffensive T2, Nr. 17), json wirft dann ValueError
        self.sent.append((name, json.loads(json.dumps(payload, allow_nan=False))))
        return len(self.sent) - self._mid_basis

    def port_wechseln_falls_noetig(self):
        pass

    # --- Teststeuerung ---

    def neuer_client(self):
        """Wie port_wechseln_falls_noetig: alter Client weg (mit seinen unbestaetigten Nachrichten), neuer Client,
        dessen mids wieder bei 1 beginnen."""
        self.client_nr = next(_CLIENT_NUMMERN)
        self._mid_basis = len(self.sent)
        self.connected = False
        self.ereignis("getrennt")

    def verbinden(self):
        self.connected = True
        self.ereignis("verbunden")

    def trennen(self):
        self.connected = False
        self.ereignis("getrennt")

    def gesendet(self, mid, client_nr=None, ok=True):
        """PUBACK des Brokers; wie der Link traegt das Ereignis die Nummer des Clients, der gesendet hat, und ob der
        Reason Code ein Erfolg war."""
        self.ereignis("gesendet", mid, self.client_nr if client_nr is None else client_nr, ok)

    def empfangen(self, name, body):
        self.ereignis("nachricht", name, json.dumps(body).encode())

    def topics(self):
        return [name for name, _ in self.sent]

    def of(self, name):
        return [payload for topic, payload in self.sent if topic == name]


class ScriptedBootstrap:
    def __init__(self) -> None:
        self.script: dict[str, list] = {"register": [], "certificate": []}
        self.calls, self.idents = [], []

    def register(self, **kwargs):
        self.calls.append(("register", kwargs))
        return self._next("register")

    def certificate(self):
        self.calls.append(("certificate", None))
        return self._next("certificate")

    def _next(self, route):
        answer = self.script[route].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class World:
    def __init__(self, tmp_path, host=None, *, access=True) -> None:
        self.dir = tmp_path / device.DEVICE_DIR
        self.host = host or FakeHost()
        self.ca = GeraeteCa()
        self.clock = Clock()
        self.links: list[FakeLink] = []
        self.client = ScriptedBootstrap()
        ident = identity.load_or_create(self.dir)
        self.zugang = self.neuer_zugang(ident)
        if access:
            bootstrap.speichern(self.dir, bootstrap.Antwort("uebernommen", self.zugang))

        def client_for(ident):
            self.client.idents.append(ident)
            return self.client

        self.device = device.Device(self.host, tmp_path, "https://accounts.example.test", clock=self.clock,
                                    wall=lambda: WALL, bootstrap_client=client_for, link_factory=self._link)

    def neuer_zugang(self, ident=None):
        ident = ident or self.device.identity
        return bootstrap.Zugang(zertifikat_fuer(ident, self.ca), bootstrap.Endpoint("mqtt.e2e.test", 8883, self.ca.pem))

    def _link(self, *args):
        self.links.append(FakeLink(*args))
        return self.links[-1]

    @property
    def link(self) -> FakeLink:
        return self.links[-1]

    def verbinden(self):
        self.device.schritt()
        self.link.verbinden()
        self.device.schritt()

    def empfangen(self, name, body):
        self.link.empfangen(name, body)
        self.device.schritt()


class FakeResponse:
    def __init__(self, status: int, body=None) -> None:
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("kein JSON")
        return self._body


class FakeSession:
    def __init__(self, *answers) -> None:
        self.answers, self.calls = list(answers), []

    def request(self, method, url, data=None, headers=None, timeout=None):
        self.calls.append((method, url, data, headers, timeout))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class Sent:
    def __init__(self, ok: bool = True) -> None:
        self.results: list[dict] = []
        self.ok = ok

    def __call__(self, body: dict) -> bool:
        self.results.append(body)
        return self.ok


def _command(kind="diagnostics", command_id="c1", payload=None):
    return {"schema": 1, "command_id": command_id, "kind": kind, "payload": {} if payload is None else payload,
            "expires_at": "2027-01-15T09:00:00+00:00"}


def _konfiguration(version=1, **changes):
    inhalt = {
        "anlage": "kunde3", "profil": "viessmann_gastherme_heizkoerper", "hebelsatz": "viessmann_vicare",
        "verteilsystem": "Heizkoerper",
        "bindung": {"treiber": {"id": "simulation", "parameter": {"heizkreis": 0}}, "thermostat": None},
        "raumfuehler": [SENSOR], "tagestick": "12:12", "abo": "aktiv", "mindest_software": "0.6.0", "software": None,
        "bindungsparameter": parameter("viessmann_vicare"),
    }
    inhalt.update(changes)
    return {"schema": 1, "version": version, **inhalt}


def _leer(version=1, mindest="0.6.0"):
    return {"schema": 1, "version": version, "anlage": None, "profil": None, "hebelsatz": None, "verteilsystem": None,
            "bindung": None, "raumfuehler": [], "tagestick": None, "abo": None, "mindest_software": mindest,
            "software": None, "bindungsparameter": None}


def _bedienung(version=1, raum_soll=21.0, **changes):
    return {"schema": 1, "version": version, "modus": "manuell", "raum_soll": raum_soll, "quelle": "portal",
            "ts": "2027-01-15T08:00:00+00:00", **changes}


def _store(tmp_path, version="0.6.0"):
    return docs.DocumentStore(tmp_path, host="gateway", software_version=version)
