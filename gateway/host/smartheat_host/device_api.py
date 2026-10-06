"""Signierte Anfragen des Updaters an die Geraete-API (Spec G3 2.1/2.2, G2b-1 4): desired und update_result.
Standardbibliothek; Signatur und Geraete-ID wie der Agent (smartheat_gateway.agent.identity/wire). Der Schluessel wird
nur gelesen: anlegen darf ihn nur der Agent (ein Schreiber je Datei)."""
import http.client
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from smartheat_gateway.agent import identity, wire


class DeviceApiError(Exception):
    """Vorlaeufiger Fehler (kein Netz, Server-Fehler, Geraet noch nicht registriert): naechster Durchlauf."""


class NotAuthenticated(DeviceApiError):
    """401: Uhr noch nicht synchron oder Geraet gesperrt; ebenfalls naechster Durchlauf."""


def load_device_key(device_dir: Path) -> ec.EllipticCurvePrivateKey:
    try:
        key = serialization.load_pem_private_key((device_dir / "key.pem").read_bytes(), password=None)
    except FileNotFoundError:
        raise DeviceApiError("Geraet noch nicht registriert (kein Schluessel)") from None
    except (OSError, ValueError) as error:
        raise DeviceApiError(f"Geraeteschluessel unlesbar ({type(error).__name__})") from None
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise DeviceApiError("Geraeteschluessel ist kein EC-Schluessel")
    return key


class DeviceApi:
    def __init__(self, base_url: str, key: ec.EllipticCurvePrivateKey, now=time.time, timeout: float = 15.0) -> None:
        self._base, self._key, self._now, self._timeout = base_url.rstrip("/"), key, now, timeout
        self.device_id = identity.device_id_for(key.public_key())

    def desired(self) -> dict:
        return self._request("desired")

    def update_result(self, version: str, result: str, reason: str) -> None:
        self._request("update_result", {"version": version, "result": result, "reason": reason})

    def _request(self, route: str, body: dict | None = None) -> dict:
        method, template = wire.ROUTES[route]
        path = template.format(device_id=self.device_id)
        data = b"" if body is None else json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
        timestamp = int(self._now())
        headers = {
            wire.HEADER_DEVICE: self.device_id,
            wire.HEADER_TIMESTAMP: str(timestamp),
            wire.HEADER_SIGNATURE: identity.sign(self._key, method, path, timestamp, data),
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self._base + path, data=data or None, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read(wire.MAX_BODY_BYTES + 1)
        except urllib.error.HTTPError as error:
            if error.code == 401:
                raise NotAuthenticated(route) from None
            raise DeviceApiError(f"{route}: HTTP {error.code}") from None
        except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
            raise DeviceApiError(f"{route}: {type(error).__name__}") from None
        if len(raw) > wire.MAX_BODY_BYTES:
            raise DeviceApiError(f"{route}: Antwort zu gross")
        try:
            answer = json.loads(raw) if raw else {}
        except ValueError:
            raise DeviceApiError(f"{route}: keine gueltige Antwort") from None
        if not isinstance(answer, dict):
            raise DeviceApiError(f"{route}: keine gueltige Antwort")
        return answer
