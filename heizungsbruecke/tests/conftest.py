"""Gemeinsame Fixtures der Kern- und HA-Host-Tests: `clock`, `make_store`, `env` (Laufzeit-Tests; Hilfen in
runtime_helpers.py) und `geraet_env` (env plus Datenordner des Geraets; Start-Tests in test_main_geraet.py).

Profile von hypothesis (Testoffensive T6, Spec §12 Nr. 20; Root-Runbook Abschnitt 3): `schnell` (Standard, 50
Beispiele, fester Seed) und `voll` (500 Beispiele, zufaelliger Seed, Blob zum Nachstellen eines Fehlers), gewaehlt per
`HYPOTHESIS_PROFILE` (`scripts/check.sh --full` setzt `voll`). Ein unbekannter Name endet mit `InvalidArgument`. Keine
Beispiel-Datenbank (`.hypothesis/` haelt nur Caches von hypothesis, per .gitignore ausgenommen), keine Frist
(ein Beispiel der Laufzeit-Tests startet eine ganze Laufzeit). Einzelne Tests setzen keine eigenen Einstellungen
(`tests/test_hypothesis_profile.py`)."""
import os
from types import SimpleNamespace

import pytest
from fakes import FakeClock, FakeHa, trigger_klasse
from hypothesis import HealthCheck, settings

import heizungsbruecke.__main__ as main_module
from heizungsbruecke import config
from heizungsbruecke.derived_sensors import DerivedSensors
from smartheat_runtime import entitlement, telemetry
from smartheat_runtime.backup_store import save_backup
from smartheat_runtime.state import StateStore

# Die Hilfsmodule sind keine Testdateien: ohne diese Anmeldung (vor ihrem ersten Import) zeigt pytest bei einem
# fehlgeschlagenen assert in ihnen keine Zwischenwerte. Deshalb steht der Import von runtime_helpers unten in `env`.
pytest.register_assert_rewrite("runtime_helpers", "geraet_helpers", "device_helpers")

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
    """Umgebung der Laufzeit-Tests (Dateipfade in tmp_path, Fake-HA, Fake-Trigger-Client, Abo-Status); gestartet wird
    mit runtime_helpers._start_bridge bzw. _start."""
    paths = {}
    for name in ("BACKUP_PATH", "FAILSAFE_PATH", "ENTITLEMENT_PATH", "DERIVED_SENSORS_PATH", "AUFRAEUMEN_PATH",
                 "RASTUNG_PATH"):
        paths[name] = tmp_path / f"{name.lower()}.json"
        monkeypatch.setattr(f"heizungsbruecke.config.{name}", paths[name])
    runtime_dir = tmp_path / "runtime"
    for name, value in (("RUNTIME_DIR", runtime_dir), ("ROOM_TARGET_PATH", runtime_dir / "room_target.json"),
                        ("ABGEMELDET_PATH", runtime_dir / "abgemeldet.json")):
        paths[name] = value
        monkeypatch.setattr(f"heizungsbruecke.config.{name}", value)
    from runtime_helpers import DERIVED

    env_derived = {"replaced": ()}
    monkeypatch.setattr(
        "heizungsbruecke.derived_sensors.ensure_all",
        lambda **kwargs: DerivedSensors({**DERIVED, "room_actual": "sensor.room_actual"}, env_derived["replaced"], "fp1"),
    )
    # Abo-Status des Konfigurationsdokuments: die Laufzeit liest ihn bei jeder Abo-Pruefung (Boot, Tick-Zustellung,
    # Fristende) frisch; queries zaehlt diese Pruefungen.
    abo_state = {"status": entitlement.ACTIVE, "queries": 0}
    trigger_clients = []
    monkeypatch.setattr("heizungsbruecke.geraet_trigger.HaTriggerClient", trigger_klasse(trigger_clients))
    return SimpleNamespace(
        clock=clock, paths=paths, abo=abo_state, ha=FakeHa(), trigger_clients=trigger_clients, derived=env_derived,
        geraet=None,
    )


@pytest.fixture
def geraet_env(env, tmp_path, monkeypatch):
    """env plus Datenordner des Geraets."""
    monkeypatch.setattr(config, "DEVICE_DATA_DIR", tmp_path / "data")
    # wo der Dokumentspeicher die Rastung schreibt (device/sicherheitssatz.json), liest sie die Laufzeit
    monkeypatch.setattr(config, "RASTUNG_PATH", tmp_path / "data" / "device" / "sicherheitssatz.json")
    monkeypatch.setattr(config, "OPTIONS_PATH", tmp_path / "options.json")
    monkeypatch.setattr(main_module.status_holder, "host", None)
    return env
