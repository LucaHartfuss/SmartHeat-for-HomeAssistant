import pytest

from heizungsbruecke.backup_store import save_backup
from heizungsbruecke.state import StateStore


class FakeClock:
    """Monotone Test-Uhr fuer RegulationWorker: steht still, bis ein Test sie vorstellt."""

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
    uebergebenen Inhalte vorher in die Dateien (wie ein vorheriger Lauf)."""
    def _make(backup: dict | None = None, failsafe: dict | None = None) -> StateStore:
        if backup is not None:
            save_backup(tmp_path / "backup.json", backup)
        if failsafe is not None:
            save_backup(tmp_path / "failsafe_state.json", failsafe)
        return StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json")
    return _make
