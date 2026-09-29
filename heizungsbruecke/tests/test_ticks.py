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
    monkeypatch.setattr(ticks, "_current_target_avg", lambda rt: None)
    rt = SimpleNamespace(manifest=MagicMock(), ha_api=MagicMock(), mqtt_client=None)

    assert ticks._attempt(rt, "s1", "daily") == delivery.Published(seq="s1", unsent=True)


def test_fallback_follow_up_keeps_the_retry_chain_alive():
    assert ticks._fallback_follow_up(delivery.Attempt(seq="s1", trigger="daily")) == delivery.Published(seq="s1")
    assert ticks._fallback_follow_up(delivery.EndEmergencyBoost()) is None
