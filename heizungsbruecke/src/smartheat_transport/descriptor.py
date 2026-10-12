"""Transport-Deskriptor und Zugangsdaten des IoT-Core-Links (Spec AWS-IoT 4.2). Den Deskriptor baut
smartheat_device.link aus der Bootstrap-Antwort; eine andere Transportart gibt es seit Phase 1 S1b nicht mehr."""
import json
from dataclasses import dataclass, field

KIND_IOT_CORE = "iot_core"
CREDENTIAL_CERTIFICATE = "certificate"
ALPN_443 = "x-amzn-mqtt-ca"
IOT_PORTS = (8883, 443)
# Gegen den Server (broker/wire.py) prueft Contract-Check 41.
DESCRIPTOR_KEYS = {
    KIND_IOT_CORE: frozenset({"kind", "host", "port", "alpn", "ca_pem", "client_id"}),
}
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

