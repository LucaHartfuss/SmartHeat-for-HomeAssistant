"""Spec 5c 1.3: Uhr aus den Host-Informationen des Supervisors."""
import pytest
import requests

from heizungsbruecke import supervisor


class Response:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


def test_host_info_reads_data(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "get", lambda url, **kw: calls.append((url, kw)) or Response(
        {"result": "ok", "data": {"dt_synchronized": True}}))
    api = supervisor.SupervisorApi(token="t")
    assert api.host_info() == {"dt_synchronized": True}
    assert calls[0][0] == "http://supervisor/host/info" and calls[0][1]["headers"]["Authorization"] == "Bearer t"
    assert calls[0][1]["timeout"] == supervisor.TIMEOUT_SECONDS


def test_host_info_without_data_is_empty(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, **kw: Response({"result": "ok", "data": None}))
    assert supervisor.SupervisorApi(token="t").host_info() == {}


def test_http_error_raises(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, **kw: Response({}, status=403))
    with pytest.raises(requests.HTTPError):
        supervisor.SupervisorApi(token="t").host_info()
