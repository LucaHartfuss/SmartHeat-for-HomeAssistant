import requests


class HomeAssistantApi:
    def __init__(self, base_url: str, token: str):
        self._base_url = base_url
        self._headers = {"Authorization": f"Bearer {token}"}

    def get_state(self, entity_id: str) -> float:
        response = requests.get(
            f"{self._base_url}/core/api/states/{entity_id}",
            headers=self._headers,
            timeout=10,
        )
        response.raise_for_status()
        return float(response.json()["state"])

    def set_number_value(self, entity_id: str, value: float) -> None:
        response = requests.post(
            f"{self._base_url}/core/api/services/number/set_value",
            headers=self._headers,
            json={"entity_id": entity_id, "value": value},
            timeout=10,
        )
        response.raise_for_status()
