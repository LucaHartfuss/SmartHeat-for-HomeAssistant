import json
from unittest.mock import patch, Mock, MagicMock

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


def test_get_state_respects_custom_api_prefix():
    api = HomeAssistantApi(base_url="http://localhost:18213", token="test-token", api_prefix="/api")
    mock_response = Mock()
    mock_response.json.return_value = {"state": "21.5"}
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.get", return_value=mock_response) as mock_get:
        result = api.get_state("sensor.test")

    assert result == 21.5
    mock_get.assert_called_once_with(
        "http://localhost:18213/api/states/sensor.test",
        headers={"Authorization": "Bearer test-token"},
        timeout=10,
    )


def test_websocket_url_appends_websocket_path_to_http_base_url():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")

    assert api._websocket_url() == "ws://supervisor/core/api/websocket"


def test_websocket_url_uses_wss_scheme_for_https_base_url():
    api = HomeAssistantApi(base_url="https://ha.example.com", token="test-token")

    assert api._websocket_url() == "wss://ha.example.com/core/api/websocket"


def test_websocket_url_respects_custom_api_prefix():
    api = HomeAssistantApi(base_url="http://localhost:18213", token="test-token", api_prefix="/api")

    assert api._websocket_url() == "ws://localhost:18213/api/websocket"


def test_call_ws_command_completes_auth_handshake_and_returns_result():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    ws = MagicMock()
    ws.recv.side_effect = [
        json.dumps({"type": "auth_required"}),
        json.dumps({"type": "auth_ok"}),
        json.dumps({"id": 1, "success": True, "result": {"id": "abc123"}}),
    ]

    with patch("heizungsbruecke.ha_api.websocket.create_connection", return_value=ws) as mock_connect:
        result = api._call_ws_command({"type": "input_number/create"})

    assert result == {"id": "abc123"}
    mock_connect.assert_called_once_with("ws://supervisor/core/api/websocket", timeout=10)
    ws.send.assert_any_call(json.dumps({"type": "auth", "access_token": "test-token"}))
    ws.send.assert_any_call(json.dumps({"id": 1, "type": "input_number/create"}))
    ws.close.assert_called_once()


def test_call_ws_command_raises_when_first_message_is_not_auth_required():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    ws = MagicMock()
    ws.recv.side_effect = [json.dumps({"type": "event"})]

    with patch("heizungsbruecke.ha_api.websocket.create_connection", return_value=ws):
        with pytest.raises(RuntimeError, match="Unerwartete erste WS-Nachricht"):
            api._call_ws_command({"type": "x"})
    ws.close.assert_called_once()


def test_call_ws_command_raises_when_auth_fails():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    ws = MagicMock()
    ws.recv.side_effect = [
        json.dumps({"type": "auth_required"}),
        json.dumps({"type": "auth_invalid"}),
    ]

    with patch("heizungsbruecke.ha_api.websocket.create_connection", return_value=ws):
        with pytest.raises(RuntimeError, match="WS-Authentifizierung fehlgeschlagen"):
            api._call_ws_command({"type": "x"})
    ws.close.assert_called_once()


def test_call_ws_command_raises_when_command_result_is_unsuccessful():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    ws = MagicMock()
    ws.recv.side_effect = [
        json.dumps({"type": "auth_required"}),
        json.dumps({"type": "auth_ok"}),
        json.dumps({"id": 1, "success": False, "error": {"message": "nope"}}),
    ]

    with patch("heizungsbruecke.ha_api.websocket.create_connection", return_value=ws):
        with pytest.raises(RuntimeError, match="WS-Kommando fehlgeschlagen"):
            api._call_ws_command({"type": "x"})
    ws.close.assert_called_once()


def test_create_input_number_sends_ws_command_and_builds_entity_id():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")

    with patch.object(
        api, "_call_ws_command", return_value={"id": "smartheat_t1_room_day_avg_2"}
    ) as mock_call:
        entity_id = api.create_input_number(
            object_id="smartheat_t1_room_day_avg", name="SmartHeat t1 Tagesmittel",
            minimum=0.0, maximum=35.0, step=0.01, initial=20.0,
        )

    assert entity_id == "input_number.smartheat_t1_room_day_avg_2"
    mock_call.assert_called_once_with({
        "type": "input_number/create",
        "name": "SmartHeat t1 Tagesmittel",
        "min": 0.0,
        "max": 35.0,
        "step": 0.01,
        "initial": 20.0,
    })


def test_set_input_number_value_posts_correct_payload():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock()
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.post", return_value=mock_response) as mock_post:
        api.set_input_number_value("input_number.smartheat_t1_room_day_avg", 21.3)

    mock_post.assert_called_once_with(
        "http://supervisor/core/api/services/input_number/set_value",
        headers={"Authorization": "Bearer test-token"},
        json={"entity_id": "input_number.smartheat_t1_room_day_avg", "value": 21.3},
        timeout=10,
    )


def test_entity_exists_returns_true_when_state_found():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(status_code=200)
    mock_response.raise_for_status.return_value = None

    with patch("heizungsbruecke.ha_api.requests.get", return_value=mock_response):
        assert api.entity_exists("sensor.dat") is True


def test_entity_exists_returns_false_on_404():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(status_code=404)

    with patch("heizungsbruecke.ha_api.requests.get", return_value=mock_response):
        assert api.entity_exists("sensor.missing") is False


def test_entity_exists_raises_on_other_http_error():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock(status_code=500)
    mock_response.raise_for_status.side_effect = RuntimeError("500")

    with patch("heizungsbruecke.ha_api.requests.get", return_value=mock_response):
        with pytest.raises(RuntimeError):
            api.entity_exists("sensor.dat")


def test_start_config_flow_posts_handler_and_returns_json():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock()
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {"type": "form", "flow_id": "f1", "data_schema": []}

    with patch("heizungsbruecke.ha_api.requests.post", return_value=mock_response) as mock_post:
        result = api._start_config_flow("statistics")

    assert result == {"type": "form", "flow_id": "f1", "data_schema": []}
    mock_post.assert_called_once_with(
        "http://supervisor/core/api/config/config_entries/flow",
        headers={"Authorization": "Bearer test-token"},
        json={"handler": "statistics", "show_advanced_options": False},
        timeout=10,
    )


def test_finish_config_flow_posts_data_to_flow_url():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    mock_response = Mock()
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {"type": "create_entry", "result": {"entry_id": "e1"}}

    with patch("heizungsbruecke.ha_api.requests.post", return_value=mock_response) as mock_post:
        result = api._finish_config_flow("f1", {"name": "x"})

    assert result == {"type": "create_entry", "result": {"entry_id": "e1"}}
    mock_post.assert_called_once_with(
        "http://supervisor/core/api/config/config_entries/flow/f1",
        headers={"Authorization": "Bearer test-token"},
        json={"name": "x"},
        timeout=10,
    )


def test_advance_config_flow_filters_fields_per_step_until_flow_completes():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    all_fields = {
        "name": "SmartHeat DART", "entity_id": "sensor.room", "state_characteristic": "mean",
        "max_age": {"hours": 24}, "sampling_size": 255, "precision": 2,
    }
    step1_response = {
        "type": "form", "flow_id": "f1",
        "data_schema": [{"name": "name"}, {"name": "entity_id"}],
    }
    step2_response = {
        "type": "form", "flow_id": "f1",
        "data_schema": [{"name": "state_characteristic"}],
    }
    step3_response = {
        "type": "form", "flow_id": "f1",
        "data_schema": [{"name": "max_age"}, {"name": "sampling_size"}, {"name": "precision"}],
    }
    final_response = {"type": "create_entry", "result": {"entry_id": "e1"}}

    with patch.object(
        api, "_finish_config_flow", side_effect=[step2_response, step3_response, final_response]
    ) as mock_finish:
        result = api._advance_config_flow(step1_response, all_fields)

    assert result == final_response
    assert mock_finish.call_args_list[0].args == ("f1", {"name": "SmartHeat DART", "entity_id": "sensor.room"})
    assert mock_finish.call_args_list[1].args == ("f1", {"state_characteristic": "mean"})
    assert mock_finish.call_args_list[2].args == (
        "f1", {"max_age": {"hours": 24}, "sampling_size": 255, "precision": 2}
    )


def test_advance_config_flow_returns_immediately_when_first_response_is_not_a_form():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    final_response = {"type": "create_entry", "result": {"entry_id": "e1"}}

    with patch.object(api, "_finish_config_flow") as mock_finish:
        result = api._advance_config_flow(final_response, {"name": "x"})

    assert result == final_response
    mock_finish.assert_not_called()


def test_find_entity_by_config_entry_returns_matching_entity_id():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    entries = [
        {"entity_id": "sensor.other", "config_entry_id": "e0"},
        {"entity_id": "sensor.dart", "config_entry_id": "e1"},
    ]

    with patch.object(api, "_call_ws_command", return_value=entries) as mock_call:
        result = api._find_entity_by_config_entry("e1")

    assert result == "sensor.dart"
    mock_call.assert_called_once_with({"type": "config/entity_registry/list"})


def test_find_entity_by_config_entry_raises_when_not_found():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")

    with patch.object(api, "_call_ws_command", return_value=[]):
        with pytest.raises(RuntimeError, match="Keine Entity"):
            api._find_entity_by_config_entry("missing")


def test_create_statistics_sensor_orchestrates_flow_and_registry_lookup():
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    flow_start_response = {"type": "form", "flow_id": "f1", "data_schema": []}
    flow_final_response = {"type": "create_entry", "result": {"entry_id": "e1"}}

    with patch.object(api, "_start_config_flow", return_value=flow_start_response) as mock_start, \
         patch.object(api, "_advance_config_flow", return_value=flow_final_response) as mock_advance, \
         patch.object(api, "_find_entity_by_config_entry", return_value="sensor.dart") as mock_find:
        result = api.create_statistics_sensor(
            name="SmartHeat t1 DART", source_entity_id="sensor.room", max_age_hours=24,
        )

    assert result == "sensor.dart"
    mock_start.assert_called_once_with("statistics")
    mock_advance.assert_called_once_with(flow_start_response, {
        "name": "SmartHeat t1 DART",
        "entity_id": "sensor.room",
        "state_characteristic": "average_step",
        "keep_last_sample": True,
        "max_age": {"hours": 24},
        "sampling_size": 255,
        "precision": 2,
    })
    mock_find.assert_called_once_with("e1")
