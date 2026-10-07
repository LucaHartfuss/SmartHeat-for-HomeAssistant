"""Duenner Client fuer die ViCare-REST-API (Spec SHG G4 1.1, Plan G4 Praezisierung 1). Jeder Aufruf der Viessmann-API
zaehlt gegen das Kontingent (G2 4.2); IAM-Aufrufe (Token) nicht. Endpunkte wie PyViCare (Referenz). In Logs und
Fehlertexten stehen nie Token, Antwortinhalte oder Seriennummern (Regel 6)."""
import logging
import math
import time

import requests

from smartheat_gateway.drivers.vicare_cloud import oauth
from smartheat_gateway.quota import QuotaGuard

logger = logging.getLogger(__name__)

PATH = "/iot/v2"
DEFAULT_RETRY_AFTER = 3600.0
MIN_RETRY_AFTER = 60.0
MAX_RETRY_AFTER = 86400.0
HTTP_TIMEOUT = 20
UNREACHABLE_TEXT = "Viessmann ist nicht erreichbar."
EXPIRED_TEXT = "Die Anmeldung bei Viessmann ist abgelaufen."


class ViCareError(Exception):
    pass


class NotAuthenticated(ViCareError):
    pass


class Unreachable(ViCareError):
    pass


class RateLimited(ViCareError):
    def __init__(self, retry_after: float) -> None:
        super().__init__("Viessmann hat die Abfragegrenze gemeldet.")
        self.retry_after = retry_after


class ReadFailed(ViCareError):
    """Eine Abfrage (GET) wurde mit 4xx abgelehnt: kein Kommando, also kein Text ueber eine abgelehnte Einstellung."""

    def __init__(self) -> None:
        super().__init__("Viessmann hat die Abfrage abgelehnt.")


class CommandRejected(ViCareError):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


class ViCareApi:
    def __init__(self, tokens: oauth.TokenStore, guard: QuotaGuard | None, *, base: str | None = None,
                 wall=time.time, session=None) -> None:
        self._tokens, self._guard, self._base, self._wall = tokens, guard, base, wall
        self._session = session or requests.Session()

    def _url(self, path: str) -> str:
        return (self._base or oauth.api_base()) + PATH + path

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        for attempt in (0, 1):
            try:
                token = self._tokens.access_token(force_refresh=attempt == 1)
            except oauth.NotLoggedIn as error:
                raise NotAuthenticated(str(error)) from error
            except oauth.TokenUnavailable as error:
                raise Unreachable(UNREACHABLE_TEXT) from error
            if self._guard is not None:
                self._guard.take()  # QuotaExhausted bis zum Aufrufer; zaehlt auch den Wiederholungsversuch
            try:
                response = self._session.request(method, self._url(path), json=body, timeout=HTTP_TIMEOUT,
                                                 headers={"Authorization": f"Bearer {token}"})
            except requests.RequestException as error:
                # Die Ausnahme nennt die URL (Seriennummer): nur den Typ loggen, nichts verketten.
                logger.warning("Viessmann-API nicht erreichbar (%s)", type(error).__name__)
                raise Unreachable(UNREACHABLE_TEXT) from None
            if response.status_code == 401 and attempt == 0:
                continue  # Token serverseitig widerrufen oder Uhr falsch: einmal erneuern
            return self._handle(method, response)
        raise NotAuthenticated(EXPIRED_TEXT)  # pragma: no cover

    @staticmethod
    def _retry_after(header: str | None) -> float:
        """Sekunden aus dem Header; unlesbar, nan, inf oder fehlend = eine Stunde, sonst auf 60 s bis 24 h begrenzt
        (inf waere eine Dauersperre in der Kontingentdatei, 0/negativ gar keine)."""
        try:
            retry = float(header) if header is not None else DEFAULT_RETRY_AFTER
        except ValueError:
            return DEFAULT_RETRY_AFTER
        if not math.isfinite(retry):
            return DEFAULT_RETRY_AFTER
        return min(max(retry, MIN_RETRY_AFTER), MAX_RETRY_AFTER)

    def _handle(self, method: str, response) -> dict:
        status = response.status_code
        if status == 429:
            retry = self._retry_after(response.headers.get("Retry-After"))
            if self._guard is not None:
                self._guard.block_until(self._wall() + retry)
            raise RateLimited(retry)
        if status == 401:
            raise NotAuthenticated(EXPIRED_TEXT)
        if status >= 500:
            raise Unreachable(UNREACHABLE_TEXT)
        if status >= 400:
            logger.warning("ViCare lehnt die Anfrage ab (HTTP %s)", status)  # nie den Body loggen (Werte, Seriennummern)
            if method == "GET":
                raise ReadFailed()
            raise CommandRejected("Die Anlage hat die Einstellung abgelehnt.")
        try:
            body = response.json()
        except ValueError as error:
            raise Unreachable("Unerwartete Antwort von Viessmann.") from error
        return body if isinstance(body, dict) else {}

    # --- Abrufe ---

    def installations(self) -> list[dict]:
        body = self._request("GET", "/equipment/installations?includeGateways=true")
        flat = []
        for installation in body.get("data") or []:
            for gateway in (installation.get("gateways") or []) if isinstance(installation, dict) else []:
                for device in (gateway.get("devices") or []) if isinstance(gateway, dict) else []:
                    if isinstance(device, dict) and "id" in installation and "serial" in gateway:
                        flat.append({"installation_id": installation["id"], "gateway_serial": gateway["serial"],
                                     "device_id": str(device.get("id")), "device_type": device.get("deviceType"),
                                     "model_id": device.get("modelId")})
        return flat

    def features(self, installation_id, gateway_serial: str, device_id: str) -> list[dict]:
        body = self._request("GET", f"/features/installations/{installation_id}/gateways/{gateway_serial}"
                                    f"/devices/{device_id}/features")
        data = body.get("data")
        return data if isinstance(data, list) else []

    def execute(self, installation_id, gateway_serial: str, device_id: str, feature: str, command: str,
                params: dict) -> None:
        self._request("POST", f"/features/installations/{installation_id}/gateways/{gateway_serial}"
                              f"/devices/{device_id}/features/{feature}/commands/{command}", params)
