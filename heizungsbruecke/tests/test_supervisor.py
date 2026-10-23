"""Uhr aus den Host-Informationen des Supervisors; Discovery-Aufrufe."""
import pytest
import requests
from fakes import HttpAntwort

from heizungsbruecke import supervisor


def test_host_info_reads_data(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "get", lambda url, **kw: calls.append((url, kw)) or HttpAntwort(
        {"result": "ok", "data": {"dt_synchronized": True}}))
    api = supervisor.SupervisorApi(token="t")
    assert api.host_info() == {"dt_synchronized": True}
    assert calls[0][0] == "http://supervisor/host/info" and calls[0][1]["headers"]["Authorization"] == "Bearer t"
    assert calls[0][1]["timeout"] == supervisor.TIMEOUT_SECONDS


def test_host_info_without_data_is_empty(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, **kw: HttpAntwort({"result": "ok", "data": None}))
    assert supervisor.SupervisorApi(token="t").host_info() == {}


def test_http_error_raises(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, **kw: HttpAntwort({}, status=403))
    with pytest.raises(requests.HTTPError):
        supervisor.SupervisorApi(token="t").host_info()


def test_discovery_anmelden_posts_the_service_and_returns_the_uuid(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "post", lambda url, **kw: calls.append((url, kw)) or HttpAntwort(
        {"result": "ok", "data": {"uuid": "u-1"}}))
    config = {"geraet_id": "shg-a", "zustand": "uebernommen"}
    assert supervisor.SupervisorApi(token="t").discovery_anmelden(config) == "u-1"
    url, kw = calls[0]
    assert url == "http://supervisor/discovery" and kw["json"] == {"service": "smartheat", "config": config}
    assert kw["headers"]["Authorization"] == "Bearer t" and kw["timeout"] == supervisor.TIMEOUT_SECONDS


@pytest.mark.parametrize("body", [{}, {"data": None}, {"data": {}}, {"data": {"uuid": 5}}, []])
def test_discovery_anmelden_without_a_uuid_raises(monkeypatch, body):
    monkeypatch.setattr(requests, "post", lambda url, **kw: HttpAntwort(body))
    with pytest.raises(ValueError):
        supervisor.SupervisorApi(token="t").discovery_anmelden({})


def test_discovery_anmelden_http_error_raises(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda url, **kw: HttpAntwort({}, status=403))
    with pytest.raises(requests.HTTPError):
        supervisor.SupervisorApi(token="t").discovery_anmelden({})


def test_discovery_entfernen_deletes_and_tolerates_404(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "delete", lambda url, **kw: calls.append((url, kw)) or HttpAntwort({}, status=404))
    supervisor.SupervisorApi(token="t").discovery_entfernen("u-1")
    assert calls[0][0] == "http://supervisor/discovery/u-1" and calls[0][1]["timeout"] == supervisor.TIMEOUT_SECONDS
    monkeypatch.setattr(requests, "delete", lambda url, **kw: HttpAntwort({}, status=200))
    supervisor.SupervisorApi(token="t").discovery_entfernen("u-1")


@pytest.mark.parametrize("status", [200, 400, 404])
def test_discovery_entfernen_counts_an_unknown_uuid_as_removed(monkeypatch, status):
    monkeypatch.setattr(requests, "delete", lambda url, **kw: HttpAntwort({}, status=status))
    supervisor.SupervisorApi(token="t").discovery_entfernen("u-1")


def test_discovery_entfernen_other_errors_raise(monkeypatch):
    monkeypatch.setattr(requests, "delete", lambda url, **kw: HttpAntwort({}, status=500))
    with pytest.raises(requests.HTTPError):
        supervisor.SupervisorApi(token="t").discovery_entfernen("u-1")


@pytest.mark.parametrize("fehler", [requests.Timeout("Bearer t"), requests.ConnectionError("Bearer t")])
def test_a_timeout_or_a_broken_connection_raises_the_request_error(monkeypatch, fehler):
    """Ein tatsaechliches Zeitlimit oder ein Verbindungsabbruch am requests-Rand: jeder Aufruf wirft die Ausnahme von
    requests (keine erfundene Antwort, kein leeres Ergebnis); die Aufrufer werten sie aus (Uhr unbekannt, Discovery
    nach der Pause erneut: test_geraet_host.py, test_discovery.py)."""
    def werfen(url, **kw):
        raise fehler

    for methode in ("get", "post", "delete"):
        monkeypatch.setattr(requests, methode, werfen)
    api = supervisor.SupervisorApi(token="t")
    with pytest.raises(type(fehler)):
        api.host_info()
    with pytest.raises(type(fehler)):
        api.discovery_anmelden({"geraet_id": "shg-a", "zustand": "uebernommen"})
    with pytest.raises(type(fehler)):
        api.discovery_entfernen("u-1")
