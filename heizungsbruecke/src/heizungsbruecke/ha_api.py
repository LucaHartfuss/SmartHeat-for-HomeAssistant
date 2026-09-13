import requests


class HomeAssistantApi:
    def __init__(self, base_url: str, token: str, api_prefix: str = "/core/api"):
        self._base_url = base_url
        self._api_prefix = api_prefix
        self._headers = {"Authorization": f"Bearer {token}"}

    def get_state(self, entity_id: str) -> float:
        real_entity_id, _, attribute = entity_id.partition("::")

        response = requests.get(
            f"{self._base_url}{self._api_prefix}/states/{real_entity_id}",
            headers=self._headers,
            timeout=10,
        )
        response.raise_for_status()
        body = response.json()

        if attribute:
            return float(body["attributes"][attribute])
        return float(body["state"])

    def set_number_value(self, entity_id: str, value: float) -> None:
        response = requests.post(
            f"{self._base_url}{self._api_prefix}/services/number/set_value",
            headers=self._headers,
            json={"entity_id": entity_id, "value": value},
            timeout=10,
        )
        response.raise_for_status()

    def create_input_number(
        self, object_id: str, name: str, minimum: float, maximum: float, step: float, initial: float
    ) -> str:
        """Legt einen input_number-Helper an -- NACH VERIFIKATION GEGEN ECHTE HA NICHT FUNKTIONSFAEHIG.

        Diese Methode entspricht dem im Task-Brief vorgesehenen Entwurf, wurde
        aber gegen einen echten HA-Core-Container (2026.9.2, siehe
        tests/test_ha_api_real_ha_integration.py) verifiziert und schlaegt dort
        mit 404 fehl: `POST .../config/input_number/config/<object_id>` existiert
        in dieser HA-Version nicht mehr als REST-Route. Quellcode-Pruefung im
        Container bestaetigt: `homeassistant/components/config/__init__.py`
        registriert keine input_number-Sektion mehr (frueher
        `config/input_number.py`, mittlerweile entfernt), und
        `homeassistant/components/input_number/__init__.py` bietet Helfer-Erzeugung
        ausschliesslich ueber `DictStorageCollectionWebsocket`, also per
        Websocket-Kommando (`input_number/create`), nicht per REST.

        Ein Wechsel auf einen Websocket-Client ist eine groessere Architektur-
        entscheidung (eigene Brainstorming-Runde noetig, siehe Design-Dokument
        Abschnitt 3.4) und wurde hier bewusst NICHT vorgenommen. Diese Methode
        bleibt als dokumentierter Platzhalter stehen, bis diese Entscheidung
        getroffen ist; siehe task-2-report.md fuer Details.
        """
        response = requests.post(
            f"{self._base_url}{self._api_prefix}/config/input_number/config/{object_id}",
            headers=self._headers,
            json={"name": name, "min": minimum, "max": maximum, "step": step, "initial": initial},
            timeout=10,
        )
        response.raise_for_status()
        return f"input_number.{object_id}"

    def set_input_number_value(self, entity_id: str, value: float) -> None:
        response = requests.post(
            f"{self._base_url}{self._api_prefix}/services/input_number/set_value",
            headers=self._headers,
            json={"entity_id": entity_id, "value": value},
            timeout=10,
        )
        response.raise_for_status()

    def entity_exists(self, entity_id: str) -> bool:
        response = requests.get(
            f"{self._base_url}{self._api_prefix}/states/{entity_id}",
            headers=self._headers,
            timeout=10,
        )
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True

    def send_notification(self, notify_service: str, message: str) -> None:
        """Calls a Home Assistant notify service, e.g. `notify.mobile_app_lucas_iphone`.

        Raises like every other method here; catching is the caller's job, since a
        failed push notification must never take down the path that triggered it.
        """
        domain, _, service = notify_service.partition(".")

        response = requests.post(
            f"{self._base_url}{self._api_prefix}/services/{domain}/{service}",
            headers=self._headers,
            json={"message": message},
            timeout=10,
        )
        response.raise_for_status()
