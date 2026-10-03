import json
import logging
from datetime import UTC, datetime, timedelta, timezone

import pytest
import requests

from heizungsbruecke import entitlement

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone(timedelta(hours=2)))


class _Response:
    def __init__(self, status_code=200, body=None, json_error=False):
        self.status_code = status_code
        self._body = body
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("kein JSON")
        return self._body


def _patch_get(monkeypatch, response=None, error=None):
    calls = []

    def fake_get(url, headers, timeout):
        calls.append((url, headers, timeout))
        if error is not None:
            raise error
        return response

    monkeypatch.setattr("heizungsbruecke.entitlement.requests.get", fake_get)
    return calls


@pytest.mark.parametrize("response,expected", [
    (_Response(200, {"active": True}), "active"),
    (_Response(200, {"active": False}), "inactive"),
    (_Response(404, {"error": "unbekannt"}), "unknown"),
    (_Response(401, {"error": "Nicht authentifiziert"}), "rejected"),
    (_Response(500, None), "unknown"),
    (_Response(503, None), "unknown"),
    (_Response(429, None), "unknown"),
    (_Response(200, {}), "unknown"),
    (_Response(200, {"active": "false"}), "unknown"),
    (_Response(200, ["active"]), "unknown"),
    (_Response(200, None, json_error=True), "unknown"),
])
def test_query_status_maps_responses(monkeypatch, response, expected):
    _patch_get(monkeypatch, response=response)
    assert entitlement.query_status("t1", "https://accounts.example", "tok") == expected


@pytest.mark.parametrize("error", [requests.Timeout("zu langsam"), requests.ConnectionError("weg"), OSError("dns")])
def test_query_status_fails_open_on_network_errors(monkeypatch, error):
    _patch_get(monkeypatch, error=error)
    assert entitlement.query_status("t1", "https://accounts.example", "tok") == "unknown"


def test_query_sends_the_installation_token_as_bearer(monkeypatch):
    calls = _patch_get(monkeypatch, _Response(200, {"active": True}))
    assert entitlement.query_status("client1", "https://accounts.example.test", "tok-123") == entitlement.ACTIVE
    assert calls == [("https://accounts.example.test/tenants/client1/status",
                      {"Authorization": "Bearer tok-123"}, 10)]


def test_query_from_options_uses_the_token_option(monkeypatch):
    calls = _patch_get(monkeypatch, response=_Response(200, {"active": False}))
    options = {"tenant_id": "t1", "accounts_api_base_url": "https://a.example", "installation_token": "tok-9"}

    assert entitlement.query_from_options(options) == entitlement.INACTIVE
    assert calls == [("https://a.example/tenants/t1/status", {"Authorization": "Bearer tok-9"}, 10)]


@pytest.mark.parametrize("token", ["", None])
def test_query_from_options_without_token_asks_nothing_and_is_unknown(monkeypatch, token):
    calls = _patch_get(monkeypatch, _Response(200, {"active": True}))
    options = {"tenant_id": "client1", "accounts_api_base_url": "https://a.example.test", "installation_token": token}
    assert entitlement.query_from_options(options) == entitlement.UNKNOWN
    assert calls == []


def test_token_is_never_logged(monkeypatch, caplog):
    _patch_get(monkeypatch, _Response(500))
    with caplog.at_level(logging.DEBUG):
        entitlement.query_status("client1", "https://a.example.test", "tok-GEHEIM")
    assert "tok-GEHEIM" not in caplog.text


def test_rejected_and_errors_never_log_the_password(monkeypatch, caplog):
    _patch_get(monkeypatch, error=requests.ConnectionError("weg"))
    with caplog.at_level(logging.DEBUG):
        entitlement.query_status("t1", "https://a.example", "sehr-geheim")
    assert "sehr-geheim" not in caplog.text


def test_rejected_401_logs_a_warning_without_the_password(monkeypatch, caplog):
    """F4 (finale Review, TP8): eine abgelehnte Anmeldung (401) blieb bisher komplett stumm --
    fuer eine Diagnose auf dem Kunden-Pi muss zumindest eine Warnung im Log stehen, ohne
    Zugangsdaten preiszugeben."""
    _patch_get(monkeypatch, response=_Response(401, {"error": "Nicht authentifiziert"}))
    with caplog.at_level(logging.DEBUG):
        result = entitlement.query_status("t1", "https://a.example", "sehr-geheim")
    assert result == entitlement.REJECTED
    assert "sehr-geheim" not in caplog.text
    assert "401" in caplog.text


@pytest.mark.parametrize("response", [
    _Response(404, {"error": "Unbekannter Tenant"}), _Response(404, json_error=True), _Response(404, None),
])
def test_every_404_counts_as_unknown(monkeypatch, caplog, response):
    # Spec TP8 3.1: der Server kennt kein 404 mehr; jeder 404 ist ein fremder Dienst.
    _patch_get(monkeypatch, response=response)

    with caplog.at_level(logging.WARNING):
        assert entitlement.query_status("t1", "https://accounts.example.test", "tok") == entitlement.UNKNOWN

    assert "404" in caplog.text


def test_mark_inactive_sets_once_and_is_idempotent(tmp_path):
    path = tmp_path / "entitlement_state.json"

    first, newly = entitlement.mark_inactive(path, NOW)
    second, newly_again = entitlement.mark_inactive(path, NOW + timedelta(days=3))

    assert (first, newly) == (NOW, True)
    assert (second, newly_again) == (NOW, False)
    assert json.loads(path.read_text()) == {"inactive_since": NOW.isoformat()}


@pytest.mark.parametrize("content", ["{kaputt", "[]", '{"inactive_since": "gestern"}',
                                     '{"inactive_since": "2026-09-01T12:00:00"}', '{"inactive_since": 5}'])
def test_corrupt_state_is_treated_as_newly_inactive(tmp_path, content):
    path = tmp_path / "entitlement_state.json"
    path.write_text(content)

    assert entitlement.load_inactive_since(path) is None
    since, newly = entitlement.mark_inactive(path, NOW)
    assert (since, newly) == (NOW, True)


def test_load_inactive_since_missing_file(tmp_path):
    assert entitlement.load_inactive_since(tmp_path / "fehlt.json") is None


def test_clear_removes_state_and_tolerates_missing_file(tmp_path):
    path = tmp_path / "entitlement_state.json"
    entitlement.mark_inactive(path, NOW)

    entitlement.clear(path)
    entitlement.clear(path)

    assert not path.exists()


def test_grace_arithmetic():
    assert entitlement.grace_end(NOW) == NOW + timedelta(days=30)
    assert entitlement.grace_expired(NOW, NOW + timedelta(days=30) - timedelta(seconds=1)) is False
    assert entitlement.grace_expired(NOW, NOW + timedelta(days=30)) is True
    # Unterschiedliche Zeitzonen-Offsets (Sommer-/Winterzeit) muessen korrekt vergleichen.
    later_utc = (NOW + timedelta(days=31)).astimezone(UTC)
    assert entitlement.grace_expired(NOW, later_utc) is True
