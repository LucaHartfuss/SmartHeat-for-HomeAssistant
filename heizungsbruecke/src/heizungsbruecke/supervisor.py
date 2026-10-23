"""Supervisor-API des Add-ons: Host-Informationen (Uhr per NTP synchron) und Discovery.
Braucht hassio_api: true in config.yaml; die Rolle default darf /host/info, /discovery ist fuer jedes Add-on offen,
das den Dienst in config.yaml erklaert. Logs nennen nie das Token (Regel 6)."""
import requests

from heizungsbruecke import discovery

TIMEOUT_SECONDS = 5


class SupervisorApi:
    def __init__(self, base_url: str = "http://supervisor", token: str = "") -> None:
        self._base, self._headers = base_url.rstrip("/"), {"Authorization": f"Bearer {token}"}

    def _get(self, path: str) -> dict:
        response = requests.get(f"{self._base}{path}", headers=self._headers, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        return data if isinstance(data, dict) else {}

    def host_info(self) -> dict:
        return self._get("/host/info")

    def discovery_anmelden(self, config: dict) -> str:
        """POST /discovery (Dienst discovery.DIENST); Antwort: UUID der Meldung. HA startet daraufhin den Flow der Integration;
        eine schon vorhandene Meldung dieses Add-ons ersetzt der Supervisor unter derselben UUID (Probe W Teil A)."""
        response = requests.post(f"{self._base}/discovery", json={"service": discovery.DIENST, "config": config},
                                 headers=self._headers, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        uuid = data.get("uuid") if isinstance(data, dict) else None
        if not isinstance(uuid, str):
            raise ValueError("Discovery ohne UUID beantwortet")
        return uuid

    def discovery_entfernen(self, uuid: str) -> None:
        response = requests.delete(f"{self._base}/discovery/{uuid}", headers=self._headers, timeout=TIMEOUT_SECONDS)
        # 404 (Supervisor 2026.09.3, Probe W Teil A) und 400 (aeltere Versionen) fuer eine unbekannte UUID: schon weg
        if response.status_code not in (200, 400, 404):
            response.raise_for_status()
