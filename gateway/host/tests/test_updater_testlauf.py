"""Der Einstieg des Docker-Tests reicht seine Werte als `Einstellungen` an `updater.main`; nur er schaltet
`allow_http` ein."""
from pathlib import Path

import updater_testlauf
from startauswahl_fake import FakeStartauswahl

from smartheat_host import updater


def test_the_test_entry_hands_its_values_to_the_updater_as_settings(monkeypatch):
    seen = []
    monkeypatch.setattr(updater, "main", lambda argv, *, einstellungen: seen.append((argv, einstellungen)))
    updater_testlauf.main(["--release-roots", "/d/test-roots.pem", "--health-host", "runtime",
                           "--health-seconds", "180", "--compose-project", "smartheat-updtest", "--once"])
    assert seen == [(["--once"], updater.Einstellungen(
        release_roots=Path("/d/test-roots.pem"), allow_http=True, health_url="http://runtime:8081/healthz",
        health_seconds=180.0, compose_project="smartheat-updtest", startauswahl=FakeStartauswahl))]


def test_the_health_address_is_the_default_of_the_device_with_the_test_host(monkeypatch):
    """Der Docker-Test fragt die Standardadresse des Geraets (Gesundheitsserver, Port 8081), nicht eine eigene."""
    assert updater.Einstellungen().health_url == "http://127.0.0.1:8081/healthz"
    seen = []
    monkeypatch.setattr(updater, "main", lambda argv, *, einstellungen: seen.append(einstellungen))
    updater_testlauf.main(["--release-roots", "x", "--health-host", "runtime", "--health-seconds", "1",
                           "--compose-project", "p"])
    assert seen[0].health_url == updater.Einstellungen().health_url.replace("127.0.0.1", "runtime")


def test_without_once_the_test_entry_runs_the_service_loop(monkeypatch):
    seen = []
    monkeypatch.setattr(updater, "main", lambda argv, *, einstellungen: seen.append(argv))
    updater_testlauf.main(["--release-roots", "x", "--health-host", "h", "--health-seconds", "1", "--compose-project", "p"])
    assert seen == [[]]


def test_the_service_does_not_start_the_test_entry():
    """Der Dienst startet `smartheat_host.updater` selbst (ohne Einstellungen); den Test-Einstieg kennt er nicht."""
    unit = (Path(__file__).resolve().parents[1] / "systemd" / "smartheat-updater.service").read_text()
    assert "ExecStart=/usr/bin/python3 -m smartheat_host.updater\n" in unit and "testlauf" not in unit
