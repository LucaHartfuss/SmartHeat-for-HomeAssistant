import requests


class HomeAssistantApi:
    def __init__(self, base_url: str, token: str):
        self._base_url = base_url
        self._headers = {"Authorization": f"Bearer {token}"}

    def get_state(self, entity_id: str) -> float:
        real_entity_id, _, attribute = entity_id.partition("::")

        response = requests.get(
            f"{self._base_url}/core/api/states/{real_entity_id}",
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
            f"{self._base_url}/core/api/services/number/set_value",
            headers=self._headers,
            json={"entity_id": entity_id, "value": value},
            timeout=10,
        )
        response.raise_for_status()
