"""Gemeinsame Fixtures der Kern- und HA-Host-Tests: `clock`, `make_store`, `env` (Laufzeit-Tests; Hilfen in
runtime_helpers.py) und `geraet_env` (env plus Datenordner des Geraets; Start-Tests in test_main_geraet.py).

Profile von hypothesis (Testoffensive T6, Spec §12 Nr. 20; Root-Runbook Abschnitt 3): `schnell` (Standard, 50
Beispiele, fester Seed) und `voll` (500 Beispiele, zufaelliger Seed, Blob zum Nachstellen eines Fehlers), gewaehlt per
`HYPOTHESIS_PROFILE` (`scripts/check.sh --full` setzt `voll`). Ein unbekannter Name endet mit `InvalidArgument`. Keine
Beispiel-Datenbank (`.hypothesis/` haelt nur Caches von hypothesis, per .gitignore ausgenommen), keine Frist
(ein Beispiel der Laufzeit-Tests startet eine ganze Laufzeit). Einzelne Tests setzen keine eigenen Einstellungen
(`tests/test_hypothesis_profile.py`)."""
import os

import pytest
from hypothesis import HealthCheck, settings

import heizungsbruecke.__main__ as main_module
from heizungsbruecke import config
from smartheat_runtime import telemetry
from smartheat_runtime.backup_store import save_backup
from smartheat_runtime.state import StateStore

# Die Hilfsmodule sind keine Testdateien: ohne diese Anmeldung (vor ihrem ersten Import) zeigt pytest bei einem
# fehlgeschlagenen assert in ihnen keine Zwischenwerte. Deshalb stehen die Importe von fakes und runtime_helpers unten in den Fixtures.
pytest.register_assert_rewrite(
    "runtime_helpers", "geraet_helpers", "device_helpers", "fakes", "host_konformitaet", "trigger_konformitaet",
)

settings.register_profile("schnell", max_examples=50, derandomize=True, database=None, deadline=None,
                          suppress_health_check=[HealthCheck.too_slow], print_blob=False)  # sonst in der CI True
settings.register_profile("voll", parent=settings.get_profile("schnell"), max_examples=500, derandomize=False,
                          print_blob=True)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "schnell"))


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
    from fakes import FakeClock

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


@pytest.fixture
def env(tmp_path, monkeypatch, clock):
    """Umgebung der Laufzeit-Tests (runtime_helpers._frische_umgebung in tmp_path); gestartet wird mit
    runtime_helpers._start_bridge bzw. _start."""
    from runtime_helpers import _frische_umgebung

    return _frische_umgebung(tmp_path, monkeypatch, clock)


@pytest.fixture
def geraet_env(env, tmp_path, monkeypatch):
    """env plus Datenordner des Geraets."""
    monkeypatch.setattr(config, "DEVICE_DATA_DIR", tmp_path / "data")
    # wo der Dokumentspeicher die Rastung schreibt (device/sicherheitssatz.json), liest sie die Laufzeit
    monkeypatch.setattr(config, "RASTUNG_PATH", tmp_path / "data" / "device" / "sicherheitssatz.json")
    monkeypatch.setattr(config, "OPTIONS_PATH", tmp_path / "options.json")
    monkeypatch.setattr(main_module.status_holder, "host", None)
    return env
