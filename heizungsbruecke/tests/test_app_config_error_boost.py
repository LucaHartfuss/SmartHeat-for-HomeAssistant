"""Audit 4, A4-10 (GK-4): Konfigurationsfehler bei laufendem Boost."""
from fakes import ACCESS_OPTIONS, FakeHa

from heizungsbruecke.__main__ import _start_bridge
from smartheat_runtime.backup_store import load_backup, save_backup

OPTIONS = {
    "tenant_id": "t", "verteilsystem": "Heizkoerper", "daily_trigger_time": "12:00", **ACCESS_OPTIONS,
    "mqtt_username": "u", "mqtt_password": "p", "room_sensors": ["sensor.room_actual"],
    "entity_room_target": "sensor.room_target", "entity_curve_current": "number.curve_current",
    "entity_shift_current": "climate.zone", "entity_min_flow": "number.min_flow",
    "entity_outdoor_temp": "sensor.outdoor_temp", "entity_heat_limit": "number.heat_limit",
    "accounts_api_base_url": "https://accounts.example.test",
    "telemetry_interval_seconds": 5,  # ungueltig -> Konfigurationsfehler
}


def _paths(tmp_path, monkeypatch):
    for name in ("BACKUP_PATH", "FAILSAFE_PATH", "ENTITLEMENT_PATH", "DERIVED_SENSORS_PATH"):
        monkeypatch.setattr(f"heizungsbruecke.config.{name}", tmp_path / f"{name.lower()}.json")
    save_backup(tmp_path / "backup_path.json", {
        "boost_active": True, "restore_point": {"curve": 1.0, "room_setpoint": 20.0, "heat_limit": 16.0}})


def test_a_config_error_takes_back_a_running_boost(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    ha = FakeHa()
    ha.states["climate.zone"] = "heat_cool"  # Zonen-Modus fuer das Vorbereiten vor dem Schreiben (FakeHa kennt ihn nicht)

    bridge = _start_bridge(OPTIONS, ha)

    assert bridge.reason == "konfigurationsfehler"
    assert ha.writes  # Rueckkehr auf den Wiederherstellungspunkt
    assert not load_backup(tmp_path / "backup_path.json").get("boost_active")


def test_a_config_error_without_restore_says_the_boost_values_remain(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    monkeypatch.setattr("heizungsbruecke.host.HaHost.sign_off_parts", lambda self: None)
    ha = FakeHa()

    _start_bridge(OPTIONS, ha)

    texts = [text for _, text in ha.persistent]
    assert any("Boost-Werten" in text for text in texts)
    assert not any("behält ihre letzten Werte" in text for text in texts)
