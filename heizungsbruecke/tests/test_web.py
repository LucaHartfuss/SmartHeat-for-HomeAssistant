from unittest.mock import Mock, patch

import requests

from heizungsbruecke.web import create_app


def _app(ha_api=None):
    return create_app(
        heizungsserver_base_url="http://heizungsserver.example",
        ha_api=ha_api or Mock(),
        supervisor_base_url="http://supervisor",
        supervisor_token="test-supervisor-token",
    )


def test_login_forwards_credentials_and_stores_token_in_session():
    app = _app()
    app.testing = True
    mock_response = Mock(status_code=200)
    mock_response.json.return_value = {"token": "abc123"}

    with patch("heizungsbruecke.web.requests.post", return_value=mock_response) as mock_post:
        client = app.test_client()
        response = client.post("/api/login", json={"email": "luca@example.com", "password": "geheim123"})

    assert response.status_code == 200
    mock_post.assert_called_once_with(
        "http://heizungsserver.example/auth/login",
        json={"email": "luca@example.com", "password": "geheim123"},
        timeout=10,
    )
    with client.session_transaction() as flask_session:
        assert flask_session["heizungsserver_token"] == "abc123"


def test_login_returns_401_when_heizungsserver_rejects_credentials():
    app = _app()
    app.testing = True
    mock_response = Mock(status_code=401)

    with patch("heizungsbruecke.web.requests.post", return_value=mock_response):
        client = app.test_client()
        response = client.post("/api/login", json={"email": "luca@example.com", "password": "falsch"})

    assert response.status_code == 401


def test_login_returns_502_when_heizungsserver_unreachable():
    app = _app()
    app.testing = True

    with patch("heizungsbruecke.web.requests.post", side_effect=requests.RequestException("down")):
        client = app.test_client()
        response = client.post("/api/login", json={"email": "luca@example.com", "password": "geheim123"})

    assert response.status_code == 502


def test_login_requires_email_and_password():
    app = _app()
    app.testing = True
    client = app.test_client()

    response = client.post("/api/login", json={"email": "luca@example.com"})

    assert response.status_code == 400


def test_tenants_requires_login():
    app = _app()
    app.testing = True
    client = app.test_client()

    response = client.get("/api/tenants")

    assert response.status_code == 401


def test_tenants_returns_list_from_heizungsserver():
    app = _app()
    app.testing = True
    login_response = Mock(status_code=200)
    login_response.json.return_value = {"token": "abc123"}
    tenants_response = Mock(status_code=200)
    tenants_response.json.return_value = [{"tenant_id": "wohnung1", "profile_id": "vaillant_gastherme_heizkoerper"}]

    client = app.test_client()
    with patch("heizungsbruecke.web.requests.post", return_value=login_response):
        client.post("/api/login", json={"email": "luca@example.com", "password": "geheim123"})

    with patch("heizungsbruecke.web.requests.get", return_value=tenants_response) as mock_get:
        response = client.get("/api/tenants")

    assert response.status_code == 200
    assert response.get_json() == [{"tenant_id": "wohnung1", "profile_id": "vaillant_gastherme_heizkoerper"}]
    mock_get.assert_called_once_with(
        "http://heizungsserver.example/accounts/me/tenants",
        headers={"Authorization": "Bearer abc123"},
        timeout=10,
    )


def test_login_returns_502_when_heizungsserver_response_missing_token():
    app = _app()
    app.testing = True
    mock_response = Mock(status_code=200)
    mock_response.json.return_value = {"error": "no token here"}

    with patch("heizungsbruecke.web.requests.post", return_value=mock_response):
        client = app.test_client()
        response = client.post("/api/login", json={"email": "luca@example.com", "password": "geheim123"})

    assert response.status_code == 502
    assert response.get_json()["error"] == "Abo-Service nicht erreichbar"


def test_login_returns_502_when_heizungsserver_response_non_json():
    app = _app()
    app.testing = True
    mock_response = Mock(status_code=200)
    mock_response.json.side_effect = ValueError("Invalid JSON")

    with patch("heizungsbruecke.web.requests.post", return_value=mock_response):
        client = app.test_client()
        response = client.post("/api/login", json={"email": "luca@example.com", "password": "geheim123"})

    assert response.status_code == 502
    assert response.get_json()["error"] == "Abo-Service nicht erreichbar"


def test_login_returns_400_when_request_body_is_non_object_json():
    app = _app()
    app.testing = True
    client = app.test_client()

    # Send a JSON list instead of a JSON object
    response = client.post(
        "/api/login",
        data='["not", "a", "dict"]',
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "E-Mail und Passwort sind erforderlich"


def test_profiles_endpoint_lists_catalog_with_verified_flag():
    app = _app()
    app.testing = True
    client = app.test_client()

    response = client.get("/api/profiles")

    assert response.status_code == 200
    body = response.get_json()
    vaillant_entry = next(e for e in body if e["profile_id"] == "vaillant_gastherme_heizkoerper")
    weishaupt_entry = next(e for e in body if e["profile_id"] == "weishaupt_waermepumpe_fussbodenheizung")
    assert vaillant_entry["verified"] is True
    assert weishaupt_entry["verified"] is False


def test_entities_endpoint_filters_by_domain():
    ha_api = Mock()
    ha_api.list_states.return_value = [
        {"entity_id": "sensor.outdoor", "attributes": {"friendly_name": "Aussen", "unit_of_measurement": "°C"}},
        {"entity_id": "number.curve", "attributes": {"friendly_name": "Kurve"}},
    ]
    app = _app(ha_api=ha_api)
    app.testing = True
    client = app.test_client()

    response = client.get("/api/entities?domain=sensor")

    assert response.status_code == 200
    assert response.get_json() == [
        {"entity_id": "sensor.outdoor", "friendly_name": "Aussen", "unit_of_measurement": "°C"},
    ]


def test_entities_endpoint_requires_domain_param():
    app = _app()
    app.testing = True
    client = app.test_client()

    response = client.get("/api/entities")

    assert response.status_code == 400


def _login(client):
    login_response = Mock(status_code=200)
    login_response.json.return_value = {"token": "abc123"}
    with patch("heizungsbruecke.web.requests.post", return_value=login_response):
        client.post("/api/login", json={"email": "luca@example.com", "password": "geheim123"})


_VALID_ENTITIES = {
    "entity_room_actual": {"entity_id": "sensor.rt", "unit_of_measurement": "°C"},
    "entity_room_target": {"entity_id": "sensor.target_rt", "unit_of_measurement": "°C"},
    "entity_outdoor_temp": {"entity_id": "sensor.outdoor", "unit_of_measurement": "°C"},
    "entity_offset_current": {"entity_id": "number.offset", "unit_of_measurement": "°C"},
    "entity_heat_limit": {"entity_id": "number.heat_limit", "unit_of_measurement": "°C"},
    "entity_curve_current": {"entity_id": "number.curve", "unit_of_measurement": None},
}


def test_complete_rejects_unverified_profile():
    app = _app()
    app.testing = True
    client = app.test_client()
    _login(client)

    response = client.post("/api/complete", json={
        "tenant_id": "wohnung1",
        "profile_id": "weishaupt_waermepumpe_fussbodenheizung",
        "entities": _VALID_ENTITIES,
    })

    assert response.status_code == 400


def test_complete_rejects_wrong_unit():
    app = _app()
    app.testing = True
    client = app.test_client()
    _login(client)

    bad_entities = dict(_VALID_ENTITIES)
    bad_entities["entity_room_actual"] = {"entity_id": "sensor.wrong", "unit_of_measurement": "%"}

    response = client.post("/api/complete", json={
        "tenant_id": "wohnung1",
        "profile_id": "vaillant_gastherme_heizkoerper",
        "entities": bad_entities,
    })

    assert response.status_code == 400


def test_complete_provisions_and_writes_options_on_success():
    app = _app()
    app.testing = True
    client = app.test_client()
    _login(client)

    provision_response = Mock(status_code=200)
    provision_response.json.return_value = {"username": "wohnung1_a1b2", "password": "geheim"}

    with patch("heizungsbruecke.web.requests.post", return_value=provision_response), \
         patch("heizungsbruecke.web.supervisor_api.set_own_options") as mock_set_options:
        response = client.post("/api/complete", json={
            "tenant_id": "wohnung1",
            "profile_id": "vaillant_gastherme_heizkoerper",
            "entities": _VALID_ENTITIES,
        })

    assert response.status_code == 200
    mock_set_options.assert_called_once()
    written_options = mock_set_options.call_args.args[2]
    assert written_options["tenant_id"] == "wohnung1"
    assert written_options["profile"] == "vaillant_gastherme_heizkoerper"
    assert written_options["entity_room_actual"] == "sensor.rt"


def test_complete_relays_provisioning_failure():
    app = _app()
    app.testing = True
    client = app.test_client()
    _login(client)

    provision_response = Mock(status_code=403)

    with patch("heizungsbruecke.web.requests.post", return_value=provision_response):
        response = client.post("/api/complete", json={
            "tenant_id": "wohnung-fremd",
            "profile_id": "vaillant_gastherme_heizkoerper",
            "entities": _VALID_ENTITIES,
        })

    assert response.status_code == 403
