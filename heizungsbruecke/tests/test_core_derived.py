from types import SimpleNamespace
from unittest.mock import MagicMock

from smartheat_core import derived
from smartheat_core.binding import VAILLANT_MYPYLLANT
from smartheat_core.safety import LocalSafety

# Bereich des Mindestvorlaufs wie bisher (min_flow_min/min_flow_max 20/30), jetzt aus LocalSafety.
SAFETY = LocalSafety(
    ranges={"curve": (0.4, 1.5), "room_setpoint": (15.0, 25.0), "heat_limit": (5.0, 23.0), "min_flow": (20.0, 30.0)},
    comfort_boost={}, emergency_boost_levers=(), arrival_threshold_k=0.5,
)


def _rt(target, live, settled=True, last_written=None):
    override = MagicMock()
    override.safety = SAFETY
    override.binding.description = VAILLANT_MYPYLLANT
    override.binding.read.side_effect = lambda lever: live() if callable(live) else live
    override.write_lever.side_effect = lambda lever, value: value
    override.settled.return_value = settled
    override.last_written.return_value = last_written
    return SimpleNamespace(
        store=SimpleNamespace(state=SimpleNamespace(stable_target=target), update=MagicMock()), override=override,
    )


def test_expected_is_room_target_clamped_and_rounded():
    assert derived.expected(_rt(20.46, 20.0)) == 20.5
    assert derived.expected(_rt(18.0, 20.0)) == 20.0
    assert derived.expected(_rt(None, 20.0)) is None


def test_sync_writes_only_on_deviation():
    rt = _rt(20.5, 20.5)
    derived.sync(rt)
    rt.override.write_lever.assert_not_called()
    rt.store.update.assert_called_once_with(min_flow_current=20.5)
    rt = _rt(21.0, 20.5)
    derived.sync(rt)
    rt.override.write_lever.assert_called_once_with("min_flow", 21.0)
    rt.store.update.assert_called_once_with(min_flow_current=21.0)


def test_sync_writes_when_live_value_unreadable():
    def _boom():
        raise RuntimeError("unavailable")
    rt = _rt(21.0, _boom)
    derived.sync(rt)
    rt.override.write_lever.assert_called_once_with("min_flow", 21.0)


def test_sync_never_raises():
    rt = _rt(21.0, 20.0)
    rt.override.write_lever.side_effect = RuntimeError("cloud")
    derived.sync(rt)


def test_unsettled_sync_trusts_the_own_last_write_not_the_lagging_ha_value():
    rt = _rt(22.0, 21.0, settled=False, last_written=22.0)  # HA zeigt noch den alten Wert
    derived.sync(rt)
    rt.override.write_lever.assert_not_called()
    rt.store.update.assert_called_once_with(min_flow_current=22.0)


def test_unsettled_sync_writes_a_change_back_even_if_ha_still_shows_it():
    rt = _rt(21.0, 21.0, settled=False, last_written=22.0)
    derived.sync(rt)
    rt.override.write_lever.assert_called_once_with("min_flow", 21.0)


def test_unsettled_sync_without_own_write_since_start_compares_with_ha():
    rt = _rt(21.0, 21.0, settled=False, last_written=None)
    derived.sync(rt)
    rt.override.write_lever.assert_not_called()
