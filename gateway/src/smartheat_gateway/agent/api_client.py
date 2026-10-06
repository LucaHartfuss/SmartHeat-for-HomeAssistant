"""Signierter Client der Geraete-API (Spec SHG G3 2.1/2.2). Jede Anfrage traegt X-SHG-Device/-Timestamp/-Signature;
der Body wird kompakt und sortiert serialisiert, damit der signierte Hash dem gesendeten Body entspricht. 401 =
NotAuthenticated (auch bei falscher Uhrzeit, Plan G2a Review Focus 1), Netz- und Serverfehler = ApiUnavailable;
eine dauerhafte Ablehnung (4xx ausser 401, 408, 429) = Rejected (Unterklasse von ApiUnavailable: eine Wiederholung
derselben Anfrage hilft nicht). Wiederholung mit Backoff ist Sache der Agent-Schleife."""
import json
import time

import requests

from smartheat_gateway.agent import wire
from smartheat_gateway.agent.identity import Identity, register_body, sign


class NotAuthenticated(Exception):
    pass


class ApiUnavailable(Exception):
    pass


class Rejected(ApiUnavailable):
    pass


_TRANSIENT_4XX = (408, 429)


class DeviceApiClient:
    def __init__(
        self, base_url: str, identity: Identity, *, now=time.time, timeout: float = 10.0, session=None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self.identity = identity
        self._now = now
        self._timeout = timeout
        self._session = session or requests.Session()

    def register(self, version: str, capabilities: dict) -> dict:
        return self._request("register", register_body(self.identity, version, capabilities))

    def commands(self) -> dict:
        return self._request("commands")

    def result(self, command_id: str, ok: bool, result: dict | None = None, error: dict | None = None) -> None:
        body = {"ok": ok, "result": result if ok else None, "error": None if ok else error}
        self._request("result", body, command_id=command_id)

    def status(self, body: dict) -> None:
        self._request("status", body)

    def notifications(self, items: list[dict]) -> None:
        self._request("notifications", {"items": items})

    def _request(self, route: str, body: dict | None = None, **params) -> dict:
        method, template = wire.ROUTES[route]
        path = template.format(device_id=self.identity.device_id, **params)
        data = b"" if body is None else json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
        timestamp = int(self._now())
        headers = {
            wire.HEADER_DEVICE: self.identity.device_id,
            wire.HEADER_TIMESTAMP: str(timestamp),
            wire.HEADER_SIGNATURE: sign(self.identity.signing_key, method, path, timestamp, data),
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = self._session.request(
                method, self._base + path, data=data or None, headers=headers, timeout=self._timeout,
            )
        except requests.RequestException as error:
            raise ApiUnavailable(type(error).__name__) from None
        if response.status_code == 401:
            raise NotAuthenticated(route)
        if 400 <= response.status_code < 500 and response.status_code not in _TRANSIENT_4XX:
            raise Rejected(f"{route}: HTTP {response.status_code}")
        if response.status_code >= 400:
            raise ApiUnavailable(f"{route}: HTTP {response.status_code}")
        try:
            return response.json() if response.content else {}
        except ValueError:
            raise ApiUnavailable(f"{route}: keine gueltige Antwort") from None
