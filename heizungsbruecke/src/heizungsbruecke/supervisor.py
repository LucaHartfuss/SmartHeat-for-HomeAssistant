"""Supervisor-API des Add-ons (Spec 5c 1.1/1.3): Host-Informationen (Uhr per NTP synchron) und Discovery (Task 8).
Braucht hassio_api: true in config.yaml; die Rolle default darf /host/info, /discovery ist fuer jedes Add-on offen,
das den Dienst in config.yaml erklaert. Logs nennen nie das Token (Regel 6)."""
import requests

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
