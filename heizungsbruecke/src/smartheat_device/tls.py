"""TLS-Weg des Geraete-Links zu AWS IoT Core: Deskriptor und Zugangsdaten, SSL-Kontext mit der CA aus dem Bootstrap,
Client-Zertifikat und ALPN bei Port 443, Einordnung von Verbindungsfehlern. paho bekommt den Kontext ueber apply(); dieses Modul importiert paho
nicht. Den Deskriptor baut link.py aus dem Bootstrap-Zugang."""
import os
import socket
import ssl
import tempfile
from dataclasses import dataclass, field

KIND_IOT_CORE = "iot_core"
CREDENTIAL_CERTIFICATE = "certificate"
ALPN_443 = "x-amzn-mqtt-ca"
IOT_PORTS = (8883, 443)  # bootstrap.py prueft den Port der Server-Antwort dagegen
CREDENTIAL_FOR_TRANSPORT = {KIND_IOT_CORE: CREDENTIAL_CERTIFICATE}


class TransportConfigError(ValueError):
    """Deskriptor oder Zugangsdaten fehlen, sind kaputt oder passen nicht zusammen. Der Text nennt nie
    einen geheimen Wert."""


@dataclass(frozen=True)
class Descriptor:
    kind: str
    host: str
    port: int
    alpn: str | None = None
    ca_pem: str | None = field(default=None, repr=False)
    client_id: str | None = None


@dataclass(frozen=True)
class Credential:
    kind: str
    certificate_pem: str | None = field(default=None, repr=False)
    private_key_pem: str | None = field(default=None, repr=False)


CONNECT_ERROR_TLS = "tls"
CONNECT_ERROR_NETWORK = "netzwerk"
CONNECT_ERROR_UNKNOWN = "unbekannt"


@dataclass(frozen=True)
class ConnectOptions:
    host: str
    port: int
    client_id: str  # der Thing-Name
    ssl_context: ssl.SSLContext = field(repr=False)


def _write_private(path: str, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(content)


def _tls_context(descriptor: Descriptor, credential: Credential) -> ssl.SSLContext:
    try:
        context = ssl.create_default_context(cadata=descriptor.ca_pem)
    except (ssl.SSLError, ValueError) as error:
        raise TransportConfigError(f"CA des Transport-Deskriptors nicht lesbar ({type(error).__name__})") from None
    # load_cert_chain liest nur Dateien: kurz in ein privates Verzeichnis (0700, Dateien 0600), danach weg.
    # Jeder OSError/ValueError (volles oder schreibgeschuetztes /tmp, NUL-Byte im PEM) wird zu einem
    # Konfigurationsfehler; nur der Klassenname geht in den Text, nie die Meldung (kann Pfade/Inhalt nennen).
    try:
        with tempfile.TemporaryDirectory(prefix="smartheat-tls-") as directory:
            cert_path, key_path = os.path.join(directory, "cert.pem"), os.path.join(directory, "key.pem")
            _write_private(cert_path, credential.certificate_pem or "")
            _write_private(key_path, credential.private_key_pem or "")
            try:
                context.load_cert_chain(cert_path, key_path)
            except ssl.SSLError as error:
                raise TransportConfigError(
                    f"Zertifikat und Schluessel passen nicht zusammen oder sind kaputt ({error.reason or type(error).__name__})"
                ) from None
    except TransportConfigError:
        raise
    except (OSError, ValueError) as error:
        raise TransportConfigError(f"Zertifikat/Schluessel nicht ablegbar ({type(error).__name__})") from None
    if descriptor.alpn:
        context.set_alpn_protocols([descriptor.alpn])
    return context


def connect_options(descriptor: Descriptor, credential: Credential) -> ConnectOptions:
    if credential.kind != CREDENTIAL_FOR_TRANSPORT[descriptor.kind]:
        raise TransportConfigError(f"Zugangsdaten {credential.kind!r} passen nicht zur Transportart {descriptor.kind!r}")
    return ConnectOptions(descriptor.host, descriptor.port, descriptor.client_id or "",
                          ssl_context=_tls_context(descriptor, credential))


def apply(client, options: ConnectOptions) -> None:
    """Anmeldung auf einen paho-Client setzen (vor connect_async)."""
    client.tls_set_context(options.ssl_context)


def classify_connect_error(error: BaseException | None) -> str:
    """TLS (Zertifikat abgelehnt, Handshake abgebrochen -- vermutlich auch ein gesperrtes
    Zertifikat) vs. Netzwerk (Broker nicht erreichbar). ssl.SSLError ist eine
    OSError-Unterklasse und wird deshalb zuerst geprueft."""
    if isinstance(error, ssl.SSLError):
        return CONNECT_ERROR_TLS
    if isinstance(error, (socket.gaierror, TimeoutError, OSError)):
        return CONNECT_ERROR_NETWORK
    return CONNECT_ERROR_UNKNOWN
