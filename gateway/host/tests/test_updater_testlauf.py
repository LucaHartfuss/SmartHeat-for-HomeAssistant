"""Der Einstieg des Docker-Tests reicht seine Werte als `Einstellungen` an `updater.main`; nur er schaltet
`allow_http` ein."""
from pathlib import Path

import updater_testlauf

from smartheat_host import updater


def test_the_test_entry_hands_its_values_to_the_updater_as_settings(monkeypatch):
    seen = []
    monkeypatch.setattr(updater, "main", lambda argv, *, einstellungen: seen.append((argv, einstellungen)))
    updater_testlauf.main(["--release-pub", "/d/test.pub", "--health-url", "http://runtime:8080/healthz",
                           "--health-seconds", "180", "--compose-project", "smartheat-updtest", "--once"])
    assert seen == [(["--once"], updater.Einstellungen(
        release_pub=Path("/d/test.pub"), allow_http=True, health_url="http://runtime:8080/healthz",
        health_seconds=180.0, compose_project="smartheat-updtest"))]


def test_without_once_the_test_entry_runs_the_service_loop(monkeypatch):
    seen = []
    monkeypatch.setattr(updater, "main", lambda argv, *, einstellungen: seen.append(argv))
    updater_testlauf.main(["--release-pub", "x", "--health-url", "u", "--health-seconds", "1", "--compose-project", "p"])
    assert seen == [[]]


def test_the_service_does_not_start_the_test_entry():
    """Der Dienst startet `smartheat_host.updater` selbst (ohne Einstellungen); den Test-Einstieg kennt er nicht."""
    unit = (Path(__file__).resolve().parents[1] / "systemd" / "smartheat-updater.service").read_text()
    assert "ExecStart=/usr/bin/python3 -m smartheat_host.updater\n" in unit and "testlauf" not in unit
