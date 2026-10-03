"""Verbindungsoptionen fuer einen paho-Client (Spec AWS-IoT 2.4, 5.1): Passwort (Mosquitto hinter
cloudflared) oder TLS mit CA aus dem Deskriptor, Client-Zertifikat und ALPN bei Port 443."""
import os
import socket
import ssl
import tempfile
from dataclasses import dataclass, field

from smartheat_transport.descriptor import (
    CREDENTIAL_CERTIFICATE,
    CREDENTIAL_FOR_TRANSPORT,
    Credential,
    Descriptor,
    TransportConfigError,
)

CONNECT_ERROR_TLS = "tls"
CONNECT_ERROR_NETWORK = "netzwerk"
CONNECT_ERROR_UNKNOWN = "unbekannt"


@dataclass(frozen=True)
class ConnectOptions:
    host: str
    port: int
    client_id: str  # "" = paho erzeugt eine (Mosquitto); bei IoT Core der Thing-Name
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    ssl_context: ssl.SSLContext | None = field(default=None, repr=False)


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
    if descriptor.alpn:
        context.set_alpn_protocols([descriptor.alpn])
    return context


def connect_options(descriptor: Descriptor, credential: Credential) -> ConnectOptions:
    if credential.kind != CREDENTIAL_FOR_TRANSPORT[descriptor.kind]:
        raise TransportConfigError(f"Zugangsdaten {credential.kind!r} passen nicht zur Transportart {descriptor.kind!r}")
    if credential.kind == CREDENTIAL_CERTIFICATE:
        return ConnectOptions(descriptor.host, descriptor.port, descriptor.client_id or "",
                              ssl_context=_tls_context(descriptor, credential))
    return ConnectOptions(descriptor.host, descriptor.port, "", username=credential.username, password=credential.password)


def apply(client, options: ConnectOptions) -> None:
    """Anmeldung auf einen paho-Client setzen (vor connect_async)."""
    if options.ssl_context is not None:
        client.tls_set_context(options.ssl_context)
    else:
        client.username_pw_set(options.username, options.password)


def classify_connect_error(error: BaseException | None) -> str:
    """TLS (Zertifikat abgelehnt, Handshake abgebrochen -- bei IoT Core vermutlich auch ein gesperrtes
    Zertifikat, Spec AN-1) vs. Netzwerk (Broker oder Tunnel nicht erreichbar). ssl.SSLError ist eine
    OSError-Unterklasse und wird deshalb zuerst geprueft."""
    if isinstance(error, ssl.SSLError):
        return CONNECT_ERROR_TLS
    if isinstance(error, (socket.gaierror, TimeoutError, OSError)):
        return CONNECT_ERROR_NETWORK
    return CONNECT_ERROR_UNKNOWN
