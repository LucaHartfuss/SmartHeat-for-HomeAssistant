"""Transport-Deskriptor und Zugangsdaten (Spec AWS-IoT 4.2). Die Integration schreibt den Deskriptor
ohne den cloudflared-Teil in die Option `transport` (Plan AWS-2, Praezisierung 2)."""
import json
from dataclasses import dataclass, field

KIND_MOSQUITTO = "mosquitto_cloudflared"
KIND_IOT_CORE = "iot_core"
CREDENTIAL_PASSWORD = "password"
CREDENTIAL_CERTIFICATE = "certificate"
ALPN_443 = "x-amzn-mqtt-ca"
IOT_PORTS = (8883, 443)
# Gegen den Server (broker/wire.py, dort mit "cloudflared") prueft Contract-Check 40.
DESCRIPTOR_KEYS = {
    KIND_MOSQUITTO: frozenset({"kind", "host", "port"}),
    KIND_IOT_CORE: frozenset({"kind", "host", "port", "alpn", "ca_pem", "client_id"}),
}
CREDENTIAL_FOR_TRANSPORT = {KIND_MOSQUITTO: CREDENTIAL_PASSWORD, KIND_IOT_CORE: CREDENTIAL_CERTIFICATE}


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
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    certificate_pem: str | None = field(default=None, repr=False)
    private_key_pem: str | None = field(default=None, repr=False)


def _object(raw) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise TransportConfigError("Transport-Deskriptor ist kein JSON") from None
    if not isinstance(raw, dict):
        raise TransportConfigError("Transport-Deskriptor ist kein Objekt")
    return raw


def parse_descriptor(raw) -> Descriptor:
    value = _object(raw)
    kind = value.get("kind")
    if kind not in DESCRIPTOR_KEYS:
        raise TransportConfigError(f"Transportart {kind!r} unbekannt")
    expected = DESCRIPTOR_KEYS[kind]
    if set(value) != expected:
        raise TransportConfigError(
            f"Transport-Deskriptor {kind}: fehlend {sorted(expected - set(value))}, "
            f"unbekannt {sorted(set(value) - expected)}"
        )
    host, port = value["host"], value["port"]
    if not isinstance(host, str) or not host:
        raise TransportConfigError("Transport-Deskriptor: host fehlt")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise TransportConfigError(f"Transport-Deskriptor: port {port!r} ungueltig")
    if kind == KIND_MOSQUITTO:
        return Descriptor(kind, host, port)
    if port not in IOT_PORTS:
        raise TransportConfigError(f"Transport-Deskriptor iot_core: port {port} (erlaubt 8883, 443)")
    expected_alpn = ALPN_443 if port == 443 else None
    if value["alpn"] != expected_alpn:
        raise TransportConfigError(f"Transport-Deskriptor iot_core: alpn {value['alpn']!r} passt nicht zu port {port}")
    ca_pem, client_id = value["ca_pem"], value["client_id"]
    if not isinstance(ca_pem, str) or "-----BEGIN CERTIFICATE-----" not in ca_pem:
        raise TransportConfigError("Transport-Deskriptor iot_core: ca_pem ist kein PEM-Zertifikat")
    if not isinstance(client_id, str) or not client_id:
        raise TransportConfigError("Transport-Deskriptor iot_core: client_id fehlt")
    return Descriptor(kind, host, port, value["alpn"], ca_pem, client_id)


def credential_for(descriptor: Descriptor, *, username=None, password=None, certificate_pem=None,
                   private_key_pem=None) -> Credential:
    """Die zum Deskriptor passenden Zugangsdaten. Felder der anderen Art muessen leer sein: sonst stammt
    die Konfiguration aus zwei Provisionierungen."""
    if CREDENTIAL_FOR_TRANSPORT[descriptor.kind] == CREDENTIAL_PASSWORD:
        if not username or not password:
            raise TransportConfigError("Transport mosquitto_cloudflared braucht mqtt_username und mqtt_password")
        if certificate_pem or private_key_pem:
            raise TransportConfigError("Transport mosquitto_cloudflared: tls_certificate/tls_private_key muessen leer sein")
        return Credential(CREDENTIAL_PASSWORD, username=username, password=password)
    if not certificate_pem or not private_key_pem:
        raise TransportConfigError("Transport iot_core braucht tls_certificate und tls_private_key")
    if username or password:
        raise TransportConfigError("Transport iot_core: mqtt_username/mqtt_password muessen leer sein")
    return Credential(CREDENTIAL_CERTIFICATE, certificate_pem=certificate_pem, private_key_pem=private_key_pem)
