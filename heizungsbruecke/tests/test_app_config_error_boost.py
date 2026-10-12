"""Audit 4, A4-10 (GK-4): Konfigurationsfehler bei laufendem Boost (Geraete-Pfad: Konfigurationsdokument mit
ungueltigem Telemetrie-Intervall)."""
from test_runtime import _start_bridge, env  # noqa: F401 - env ist eine Fixture

from smartheat_runtime.backup_store import load_backup, save_backup

FEHLER = {"entity_shift_current": "climate.zone", "telemetry_interval_seconds": 5}  # ungueltig -> Konfigurationsfehler


def _boost(env):
    save_backup(env.paths["BACKUP_PATH"], {
        "boost_active": True, "restore_point": {"curve": 1.0, "room_setpoint": 20.0, "heat_limit": 16.0}})


def test_a_config_error_takes_back_a_running_boost(env):
    _boost(env)
    env.ha.states["climate.zone"] = "heat_cool"  # Zonen-Modus fuer das Vorbereiten vor dem Schreiben (FakeHa kennt ihn nicht)

    bridge = _start_bridge(env, **FEHLER)

    assert bridge.reason == "konfigurationsfehler"
    assert env.ha.writes  # Rueckkehr auf den Wiederherstellungspunkt
    assert not load_backup(env.paths["BACKUP_PATH"]).get("boost_active")


def test_a_config_error_without_restore_says_the_boost_values_remain(env, monkeypatch):
    _boost(env)
    monkeypatch.setattr("heizungsbruecke.host.HaHost.sign_off_parts", lambda self: None)

    _start_bridge(env, **FEHLER)

    texts = [text for _, text in env.ha.persistent]
    assert any("Boost-Werten" in text for text in texts)
    assert not any("behält ihre letzten Werte" in text for text in texts)
