"""Tick-Zustellung (ticks.py) auf Einheitsebene; der volle Ablauf steht in test_runtime.py."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from heizungsbruecke import delivery, ticks


def test_end_emergency_boost_keeps_running_comfort_boost():
    rt = SimpleNamespace(override=MagicMock(), store=SimpleNamespace(state=SimpleNamespace(boost_active=True)))

    assert ticks._execute(rt, delivery.EndEmergencyBoost()) is None

    rt.override.set_boosts.assert_called_once_with(comfort=True, emergency=False)


def test_end_emergency_boost_without_any_boost_asks_override_to_end_both():
    rt = SimpleNamespace(override=MagicMock(), store=SimpleNamespace(state=SimpleNamespace(boost_active=False)))

    ticks._execute(rt, delivery.EndEmergencyBoost())

    rt.override.set_boosts.assert_called_once_with(comfort=False, emergency=False)


def test_attempt_without_mqtt_client_reports_unsent(monkeypatch):
    monkeypatch.setattr(ticks, "read_snapshot_roles", lambda *a, **kw: SimpleNamespace(invalid_roles=(), roles={}))
    rt = SimpleNamespace(
        manifest=SimpleNamespace(entity_ids={"shift_current": "number.shift"}), ha_api=MagicMock(),
        mqtt_client=None, store=SimpleNamespace(state=SimpleNamespace(shift_current=21.0)),
    )

    assert ticks._attempt(rt, "s1", "daily") == delivery.Published(seq="s1", unsent=True)


def test_snapshot_uses_restore_point_when_zone_is_off(monkeypatch):
    captured = {}

    def _read(manifest, ha_api, computed_values):
        captured.update(computed_values)
        return SimpleNamespace(invalid_roles=(), roles={})

    monkeypatch.setattr(ticks, "read_snapshot_roles", _read)
    ha_api = MagicMock()
    ha_api.get_state.return_value = 0.0
    rt = SimpleNamespace(
        manifest=SimpleNamespace(entity_ids={"shift_current": "climate.zone::temperature"}), ha_api=ha_api,
        mqtt_client=None, store=SimpleNamespace(state=SimpleNamespace(shift_current=21.0)),
    )
    ticks._attempt(rt, "s1", "daily")
    assert captured == {"shift_current": 21.0}


def test_fallback_follow_up_keeps_the_retry_chain_alive():
    assert ticks._fallback_follow_up(delivery.Attempt(seq="s1", trigger="daily")) == delivery.Published(seq="s1")
    assert ticks._fallback_follow_up(delivery.EndEmergencyBoost()) is None
