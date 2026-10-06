import pytest
from fake_device_api import FakeDeviceApi


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
