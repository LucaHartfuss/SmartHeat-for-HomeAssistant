from unittest.mock import patch, Mock

import pytest

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


def test_get_state_with_attribute_reference_reads_named_attribute():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock()
    mock_response.json.return_value = {
        "state": "heat",
        "attributes": {"current_temperature": 19.5, "temperature": 21.0},
    }
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.get", return_value=mock_response) as mock_get:
        result = api.get_state("climate.wohnzimmer_thermostat::current_temperature")

    assert result == 19.5
    mock_get.assert_called_once_with(
        "http://supervisor/core/api/states/climate.wohnzimmer_thermostat",
        headers={"Authorization": "Bearer test-token"},
        timeout=10,
    )


def test_get_state_with_target_attribute_reference_reads_temperature():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock()
    mock_response.json.return_value = {
        "state": "heat",
        "attributes": {"current_temperature": 19.5, "temperature": 21.0},
    }
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.get", return_value=mock_response):
        result = api.get_state("climate.wohnzimmer_thermostat::temperature")

    assert result == 21.0


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


def test_send_notification_posts_to_the_split_notify_service():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock()
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.post", return_value=mock_response) as mock_post:
        api.send_notification("notify.mobile_app_lucas_iphone", "Sensor defekt")

    mock_post.assert_called_once_with(
        "http://supervisor/core/api/services/notify/mobile_app_lucas_iphone",
        headers={"Authorization": "Bearer test-token"},
        json={"message": "Sensor defekt"},
        timeout=10,
    )


def test_send_notification_raises_on_http_error():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock()
    mock_response.raise_for_status.side_effect = RuntimeError("500")

    with patch("heizungsbruecke.ha_api.requests.post", return_value=mock_response):
        with pytest.raises(RuntimeError):
            api.send_notification("notify.mobile_app_lucas_iphone", "Sensor defekt")
