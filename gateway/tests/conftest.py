import pytest
from configs import SENSOR, THERMOSTAT
from fake_device_api import FakeDeviceApi
from fake_vicare.server import GOOD_CODE, FakeVicare
from fake_z2m import FakeZigbee2Mqtt
from fakes import FakeBus

from smartheat_gateway.agent import identity
from smartheat_gateway.agent.context import AgentContext
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


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def make_store(tmp_path):
    """StateStore auf tmp_path/backup.json und tmp_path/failsafe_state.json; schreibt die
    uebergebenen Inhalte vorher in die Dateien (wie ein vorheriger Lauf).
    Woertlich aus heizungsbruecke/tests/conftest.py uebernommen."""
    def _make(backup: dict | None = None, failsafe: dict | None = None) -> StateStore:
        if backup is not None:
            save_backup(tmp_path / "backup.json", backup)
        if failsafe is not None:
            save_backup(tmp_path / "failsafe_state.json", failsafe)
        return StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json")
    return _make


@pytest.fixture
def data_dir(tmp_path):
    """Nachbildung von /data (SHG_DATA_DIR)."""
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
def api():
    server = FakeDeviceApi()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def vicare_ctx(data_dir, clock, monkeypatch):
    """Agent-Kontext mit angemeldetem vicare_cloud-Treiber gegen den Fake-ViCare-Server: (ctx, server, clock).
    Die Wanduhr des Kontexts folgt der Test-Uhr (ctx.wall = 1_000_000 + clock())."""
    server = FakeVicare()
    base = server.start()
    for key, value in {"SHG_TEST_ENDPOINTS": "1", "SHG_VICARE_IAM_BASE": base, "SHG_VICARE_API_BASE": base}.items():
        monkeypatch.setenv(key, value)
    paths = Paths(data_dir)
    login = ViCareCloudDriver({}, paths, clock=clock)
    login.login_begin({"client_id": "c", "redirect_uri": "https://portal.test/oauth/callback"})
    login.login_finish({"code": GOOD_CODE, "redirect_uri": "https://portal.test/oauth/callback"})
    bus = FakeBus()
    z2m = FakeZigbee2Mqtt(bus)
    mirror = ZigbeeMirror(bus, clock)
    mirror.start()
    z2m.bridge(online=True)
    z2m.add_sensor(SENSOR)
    z2m.add_thermostat(THERMOSTAT)
    context = AgentContext(paths, bus, mirror, identity.load_or_create(paths), clock=clock)
    context.wall = lambda: 1_000_000 + clock()
    context.start()
    yield context, server, clock
    server.stop()
