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
