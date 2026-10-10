from pathlib import Path

import pytest
from configs import THERMOSTAT
from fake_vicare.server import GOOD_CODE, FakeVicare
from fakes import FakeBus

from smartheat_gateway.drivers.vicare_cloud.driver import ViCareCloudDriver
from smartheat_gateway.paths import Paths
from smartheat_gateway.zigbee import ZigbeeMirror
from smartheat_runtime.backup_store import save_backup
from smartheat_runtime.state import StateStore


class FakeClock:
    """Monotone Test-Uhr (wie im Add-on): steht still, bis ein Test sie vorstellt."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _kein_prozessneustart(tmp_path, monkeypatch):
    """Nie os.execv im Test (runtime_main.attach leitet abo.restart_process um; monkeypatch stellt das Original nach
    jedem Test wieder her) und nie das echte Lebenszeichen unter /tmp."""
    def _neustart():
        raise SystemExit(0)

    monkeypatch.setattr("smartheat_runtime.abo.restart_process", _neustart)
    monkeypatch.setenv("SHG_ALIVE_FILE", str(tmp_path / "alive"))


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def make_store(tmp_path):
    """StateStore auf <directory>/backup.json und <directory>/failsafe_state.json (Vorgabe: tmp_path); schreibt die
    uebergebenen Inhalte vorher in die Dateien (wie ein vorheriger Lauf). Wie heizungsbruecke/tests/conftest.py, nur
    mit wahlweisem Verzeichnis, damit zwei Staende im selben Test unabhaengig voneinander bleiben."""
    def _make(backup: dict | None = None, failsafe: dict | None = None, directory: Path | None = None) -> StateStore:
        base = directory if directory is not None else tmp_path
        base.mkdir(parents=True, exist_ok=True)
        if backup is not None:
            save_backup(base / "backup.json", backup)
        if failsafe is not None:
            save_backup(base / "failsafe_state.json", failsafe)
        return StateStore(base / "backup.json", base / "failsafe_state.json")
    return _make


@pytest.fixture
def data_dir(tmp_path):
    """Nachbildung von /data (SHG_DATA_DIR)."""
    path = tmp_path / "data"
    path.mkdir()
    return path


VICARE_PARAMETER = {"installation_id": 2012345, "gateway_serial": "7637415000000001", "device_id": "0",
                    "heizkreis": 0, "poll_seconds": 300.0}


@pytest.fixture
def vicare_kontext(data_dir, clock, monkeypatch):
    """Befehls-Kontext mit angemeldetem vicare_cloud-Treiber gegen den Fake-ViCare-Server und einer Konfiguration fuer
    diesen Treiber: (ctx, server, clock). Die Wanduhr folgt der Test-Uhr (ctx.wall = 1_000_000 + clock())."""
    from configs import konfiguration

    from smartheat_gateway import befehle

    server = FakeVicare()
    base = server.start()
    for key, value in {"SHG_TEST_ENDPOINTS": "1", "SHG_VICARE_IAM_BASE": base, "SHG_VICARE_API_BASE": base}.items():
        monkeypatch.setenv(key, value)
    paths = Paths(data_dir)
    login = ViCareCloudDriver({}, paths, clock=clock)
    login.login_begin({"client_id": "c", "redirect_uri": "https://portal.test/oauth/callback"})
    login.login_finish({"code": GOOD_CODE, "redirect_uri": "https://portal.test/oauth/callback"})
    bus = FakeBus()
    mirror = ZigbeeMirror(bus, clock)
    mirror.start()
    konf = konfiguration(bindung={"treiber": {"id": "vicare_cloud", "parameter": VICARE_PARAMETER},
                                  "thermostat": THERMOSTAT})
    context = befehle.Kontext(paths, mirror, konfiguration=lambda: konf, runtime_status=lambda: None,
                              neustart=lambda: None, clock=clock, wall=lambda: 1_000_000 + clock())
    yield context, server, clock
    server.stop()
