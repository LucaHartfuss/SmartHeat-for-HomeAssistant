"""HTTPS-Bootstrap (Spec 5b 1): register mit CSR liefert Zertifikat, Broker-Endpunkt und Broker-CA, certificate ist
Rettungsweg und Erneuerung. Signiert wie der Vertrag G3 (SHG1, 300-s-Fenster). Der Server antwortet bei jedem
Anmeldefehler gleich (401), auch fuer ein gesperrtes Geraet: das Geraet wertet "401 auf certificate und register" als
gesperrt (Plan S1) und versucht es nach GESPERRT_SECONDS erneut, aber nur bei synchroner Uhr. Ohne synchrone Uhr (Pi
ohne Echtzeituhr nach einem Stromausfall) liegt der Zeitstempel womoeglich ausserhalb des Fensters: dann kein Urteil,
sondern ein Versuch nach Backoff (Plan-Praezisierung). Zertifikat, Endpunkt und Uebernahmestatus liegen unter
<daten>/device/; der Broker-CA wird vertraut, weil sie ueber die HTTPS-Verbindung kam (Plan S1). Ein Zertifikat, das
nicht zum eigenen TLS-Schluessel passt, wird nie gespeichert. Logs nennen Route und Status, nie Inhalte (Regel 6)."""
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import requests

from smartheat_device import identity as identity_module
from smartheat_device import wire
from smartheat_device.documents import read_json, write_json, write_text

logger = logging.getLogger(__name__)

CERTIFICATE_FILE = "certificate.pem"
ENDPOINT_FILE = "endpoint.json"
STATE_FILE = "bootstrap.json"
GESPERRT_SECONDS = 6 * 3600
RETRY_MIN_SECONDS = 30
RETRY_MAX_SECONDS = 900
TIMEOUT_SECONDS = 10.0
_TRANSIENT_4XX = (408, 429)
_PEM_CERT = "-----BEGIN CERTIFICATE-----"


class BootstrapError(Exception):
    """Bootstrap gescheitert; der Text nennt Route und Status, nie Antwortinhalte."""


class NotAuthenticated(BootstrapError):
    """401: Anmeldefehler, Zeitstempel ausserhalb des Fensters oder gesperrt (fuer alle gleich, Spec 1)."""


class Unavailable(BootstrapError):
    """Netz, Zeitueberschreitung, 5xx, 408/429 oder eine unbrauchbare Antwort: spaeter erneut."""


class Rejected(BootstrapError):
    """Anderes 4xx: die Anfrage selbst ist falsch; eine sofortige Wiederholung hilft nicht."""


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    ca_pem: str = field(repr=False)
    alpn: str | None = None


@dataclass(frozen=True)
class Zugang:
    certificate_pem: str = field(repr=False)
    endpoint: Endpoint


@dataclass(frozen=True)
class Antwort:
    device_state: str | None
    zugang: Zugang


def _antwort(body) -> Antwort:
    if not isinstance(body, dict) or any(key not in body for key in wire.BOOTSTRAP_RESPONSE):
        raise Unavailable("Bootstrap-Antwort unvollstaendig")
    host, port, ca, cert = body["mqtt_endpoint"], body["mqtt_port"], body["mqtt_ca"], body["certificate"]
    alpn = body.get("mqtt_alpn")
    if not (isinstance(host, str) and host and isinstance(port, int) and not isinstance(port, bool)
            and 0 < port < 65536 and isinstance(ca, str) and _PEM_CERT in ca and isinstance(cert, str)
            and _PEM_CERT in cert and (alpn is None or isinstance(alpn, str))):
        raise Unavailable("Bootstrap-Antwort ungueltig")
    state = body["device_state"] if body["device_state"] in wire.DEVICE_STATES else None
    return Antwort(state, Zugang(cert, Endpoint(host, port, ca, alpn or None)))


class BootstrapClient:
    """Signierte Anfragen an register und certificate (Signatur wie die Polling-API, G3 2.1)."""

    def __init__(self, base_url: str, ident: identity_module.Identity, *, session=None,
                 now: Callable[[], float] = time.time, timeout: float = TIMEOUT_SECONDS) -> None:
        self._base = base_url.rstrip("/")
        self._ident, self._now, self._timeout = ident, now, timeout
        self._session = session or requests.Session()

    def register(self, *, version: str, host: str, capabilities: dict) -> Antwort:
        body = identity_module.register_body(self._ident, version=version, host=host, capabilities=capabilities)
        return _antwort(self._request("register", body))

    def certificate(self) -> Antwort:
        return _antwort(self._request("certificate", {"csr": identity_module.csr_pem(self._ident)}))

    def _request(self, route: str, body: dict):
        method, template = wire.ROUTES[route]
        path = template.format(device_id=self._ident.device_id)
        data = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
        timestamp = int(self._now())
        headers = {
            wire.HEADER_DEVICE: self._ident.device_id,
            wire.HEADER_TIMESTAMP: str(timestamp),
            wire.HEADER_SIGNATURE: identity_module.sign(self._ident.signing_key, method, path, timestamp, data),
            "Content-Type": "application/json",
        }
        try:
            response = self._session.request(method, self._base + path, data=data, headers=headers,
                                             timeout=self._timeout)
        except requests.RequestException as error:
            raise Unavailable(f"{route}: {type(error).__name__}") from None
        if response.status_code == 401:
            raise NotAuthenticated(route)
        if 400 <= response.status_code < 500 and response.status_code not in _TRANSIENT_4XX:
            raise Rejected(f"{route}: HTTP {response.status_code}")
        if response.status_code >= 300:
            raise Unavailable(f"{route}: HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise Unavailable(f"{route}: keine gueltige Antwort") from None


def device_state(device_dir: Path) -> str | None:
    raw = read_json(device_dir / STATE_FILE)
    state = raw.get("device_state") if isinstance(raw, dict) else None
    return state if state in wire.DEVICE_STATES else None


def device_state_speichern(device_dir: Path, state: str | None) -> None:
    if state in wire.DEVICE_STATES and state != device_state(device_dir):
        write_json(device_dir / STATE_FILE, {"device_state": state})


def speichern(device_dir: Path, antwort: Antwort) -> None:
    """Endpunkt zuerst, Zertifikat danach (seine Datei heisst: Bootstrap fertig), dann der Uebernahmestatus."""
    endpoint = antwort.zugang.endpoint
    write_json(device_dir / ENDPOINT_FILE,
               {"host": endpoint.host, "port": endpoint.port, "ca_pem": endpoint.ca_pem, "alpn": endpoint.alpn})
    write_text(device_dir / CERTIFICATE_FILE, antwort.zugang.certificate_pem)
    device_state_speichern(device_dir, antwort.device_state)


def laden(device_dir: Path) -> Zugang | None:
    raw = read_json(device_dir / ENDPOINT_FILE)
    try:
        cert = (device_dir / CERTIFICATE_FILE).read_text()
    except OSError:
        return None
    if not (isinstance(raw, dict) and isinstance(raw.get("host"), str) and isinstance(raw.get("port"), int)
            and isinstance(raw.get("ca_pem"), str) and _PEM_CERT in cert):
        return None
    alpn = raw.get("alpn")
    return Zugang(cert, Endpoint(raw["host"], raw["port"], raw["ca_pem"], alpn if isinstance(alpn, str) else None))


class Bootstrap:
    """Ablauf im Geraete-Thread: holen(rettung=False) beim ersten Start (register), holen(rettung=True) als Rettungsweg
    (certificate, bei 401 register). Ein neuer Zugang wird gespeichert und zurueckgegeben; None heisst: spaeter erneut
    (faellig() sagt wann). Lieferte register ein Zertifikat fuer einen fremden Schluessel, nimmt der naechste Versuch den
    Rettungsweg (certificate stellt fuer den eigenen CSR aus), sonst kaeme bei jedem Versuch dasselbe Zertifikat."""

    def __init__(self, device_dir: Path, client: Callable[[], BootstrapClient], *, register_args: Callable[[], dict],
                 uhr_synchron: Callable[[], bool | None], identity: Callable[[], identity_module.Identity],
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._dir, self._client, self._register_args = device_dir, client, register_args
        self._uhr_synchron, self._identity, self._clock = uhr_synchron, identity, clock
        self.gesperrt = False
        self.uhr_ungewiss = False
        self._failures = 0
        self._next = 0.0
        self._fremdes_zertifikat = False

    def faellig(self) -> bool:
        return self._clock() >= self._next

    def holen(self, *, rettung: bool) -> Zugang | None:
        client = self._client()
        try:
            if rettung or self._fremdes_zertifikat:
                try:
                    antwort = client.certificate()
                except NotAuthenticated:
                    logger.warning("Zertifikat erneuern: nicht authentifiziert, registriere neu")
                    antwort = client.register(**self._register_args())
            else:
                antwort = client.register(**self._register_args())
        except NotAuthenticated:
            return self._nicht_angemeldet()
        except BootstrapError as error:
            logger.warning("Bootstrap gescheitert (%s), naechster Versuch spaeter", error)
            return self._spaeter()
        if not identity_module.passt_zum_schluessel(self._identity(), antwort.zugang.certificate_pem):
            logger.error("Bootstrap: Zertifikat passt nicht zum eigenen Schluessel, verworfen")
            self._fremdes_zertifikat = True
            return self._spaeter()
        speichern(self._dir, antwort)
        self.gesperrt = False
        self.erfolg()
        return antwort.zugang

    def erfolg(self) -> None:
        """Der Zugang funktioniert (neu geholt oder der Link hat mit dem bisherigen verbunden): Backoff zuruecksetzen.
        Eine Sperre hebt nur ein neu geholter Zugang auf."""
        self.uhr_ungewiss = self._fremdes_zertifikat = False
        self._failures, self._next = 0, 0.0

    def spaeter(self) -> None:
        """Fehlschlag ausserhalb des Bootstraps (Link startet mit dem Zugang nicht): naechster Versuch erst nach dem
        normalen Backoff, sonst holte jeder Takt ein neues Zertifikat."""
        self._spaeter()

    def _nicht_angemeldet(self) -> None:
        if self._uhr_synchron() is not True:
            self.uhr_ungewiss = True
            logger.warning("Bootstrap: nicht authentifiziert, Uhr nicht synchron - kein Urteil, erneuter Versuch spaeter")
            return self._spaeter()
        self.gesperrt = True
        self._next = self._clock() + GESPERRT_SECONDS
        logger.error("Bootstrap: Geraet gesperrt (401 bei synchroner Uhr), naechster Versuch in %d h",
                     GESPERRT_SECONDS // 3600)
        return None

    def _spaeter(self) -> None:
        self._failures += 1
        self._next = self._clock() + min(RETRY_MAX_SECONDS, RETRY_MIN_SECONDS * 2 ** min(self._failures - 1, 5))
        return None
