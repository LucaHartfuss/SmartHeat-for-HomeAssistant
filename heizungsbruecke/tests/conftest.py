"""Gemeinsame Fixtures der Kern- und HA-Host-Tests.

Profile von hypothesis (Testoffensive T6, Spec §12 Nr. 20; Root-Runbook Abschnitt 3): `schnell` (Standard, 50
Beispiele, fester Seed) und `voll` (500 Beispiele, zufaelliger Seed, Blob zum Nachstellen eines Fehlers), gewaehlt per
`HYPOTHESIS_PROFILE` (`scripts/check.sh --full` setzt `voll`). Ein unbekannter Name endet mit `InvalidArgument`. Keine
Beispiel-Datenbank (`.hypothesis/` haelt nur Caches von hypothesis, per .gitignore ausgenommen), keine Frist
(ein Beispiel der Laufzeit-Tests startet eine ganze Laufzeit). Einzelne Tests setzen keine eigenen Einstellungen
(`tests/test_hypothesis_profile.py`)."""
import os

import pytest
from hypothesis import HealthCheck, settings

from smartheat_runtime import telemetry
from smartheat_runtime.backup_store import save_backup
from smartheat_runtime.state import StateStore

settings.register_profile("schnell", max_examples=50, derandomize=True, database=None, deadline=None,
                          suppress_health_check=[HealthCheck.too_slow], print_blob=False)  # sonst in der CI True
settings.register_profile("voll", parent=settings.get_profile("schnell"), max_examples=500, derandomize=False,
                          print_blob=True)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "schnell"))


class FakeClock:
    """Monotone Test-Uhr fuer RegulationWorker: steht still, bis ein Test sie vorstellt."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _telemetrie_warnzustand_zuruecksetzen():
    """Die Warn-Zustaende der Telemetrie sind modulweit (eine Warnung je Wechsel der Einheit bzw. je Ausfall von
    room_actual): je Test frisch, damit kein Zustand ueber Testdateien und xdist-Worker haengen bleibt."""
    telemetry._EINHEIT_GEWARNT.clear()
    telemetry._AUSFALL_GEWARNT.clear()
    yield
    telemetry._EINHEIT_GEWARNT.clear()
    telemetry._AUSFALL_GEWARNT.clear()


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
