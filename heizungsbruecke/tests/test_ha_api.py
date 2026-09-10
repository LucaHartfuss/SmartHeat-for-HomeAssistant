from unittest.mock import patch, Mock

from heizungsbruecke.ha_api import HomeAssistantApi


def test_get_state_returns_parsed_float():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock()
    mock_response.json.return_value = {"state": "21.5"}
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.get", return_value=mock_response) as mock_get:
        result = api.get_state("climate.wohnzimmer_thermostat")

    assert result == 21.5
    mock_get.assert_called_once_with(
        "http://supervisor/core/api/states/climate.wohnzimmer_thermostat",
        headers={"Authorization": "Bearer test-token"},
        timeout=10,
    )


def test_set_number_value_posts_correct_payload():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock()
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.post", return_value=mock_response) as mock_post:
        api.set_number_value("number.weishaupt_heizkurve_steigung", 0.72)

    mock_post.assert_called_once_with(
        "http://supervisor/core/api/services/number/set_value",
        headers={"Authorization": "Bearer test-token"},
        json={"entity_id": "number.weishaupt_heizkurve_steigung", "value": 0.72},
        timeout=10,
    )
