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
