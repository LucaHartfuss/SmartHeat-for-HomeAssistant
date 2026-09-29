from types import SimpleNamespace
from unittest.mock import MagicMock

from heizungsbruecke import min_flow

OPTIONS = {"min_flow_min": 20.0, "min_flow_max": 30.0}


def _rt(target, live, settled=True, last_written=None):
    ha_api = MagicMock()
    ha_api.get_state.side_effect = lambda ref: live() if callable(live) else live
    override = MagicMock()
    override.write_min_flow.side_effect = lambda value: value
    override.settled.return_value = settled
    override.last_written.return_value = last_written
    return SimpleNamespace(
        store=SimpleNamespace(state=SimpleNamespace(stable_target=target), update=MagicMock()),
        manifest=SimpleNamespace(entity_ids={"min_flow": "number.mf"}), ha_api=ha_api, override=override,
        options=OPTIONS,
    )


def test_expected_is_room_target_clamped_and_rounded():
    assert min_flow.expected(_rt(20.46, 20.0)) == 20.5
    assert min_flow.expected(_rt(18.0, 20.0)) == 20.0
    assert min_flow.expected(_rt(None, 20.0)) is None


def test_sync_writes_only_on_deviation():
    rt = _rt(20.5, 20.5)
    min_flow.sync(rt)
    rt.override.write_min_flow.assert_not_called()
    rt.store.update.assert_called_once_with(min_flow_current=20.5)
    rt = _rt(21.0, 20.5)
    min_flow.sync(rt)
    rt.override.write_min_flow.assert_called_once_with(21.0)
    rt.store.update.assert_called_once_with(min_flow_current=21.0)


def test_sync_writes_when_live_value_unreadable():
    def _boom():
        raise RuntimeError("unavailable")
    rt = _rt(21.0, _boom)
    min_flow.sync(rt)
    rt.override.write_min_flow.assert_called_once_with(21.0)


def test_sync_never_raises():
    rt = _rt(21.0, 20.0)
    rt.override.write_min_flow.side_effect = RuntimeError("cloud")
    min_flow.sync(rt)


def test_unsettled_sync_trusts_the_own_last_write_not_the_lagging_ha_value():
    rt = _rt(22.0, 21.0, settled=False, last_written=22.0)  # HA zeigt noch den alten Wert
    min_flow.sync(rt)
    rt.override.write_min_flow.assert_not_called()
    rt.store.update.assert_called_once_with(min_flow_current=22.0)


def test_unsettled_sync_writes_a_change_back_even_if_ha_still_shows_it():
    rt = _rt(21.0, 21.0, settled=False, last_written=22.0)
    min_flow.sync(rt)
    rt.override.write_min_flow.assert_called_once_with(21.0)


def test_unsettled_sync_without_own_write_since_start_compares_with_ha():
    rt = _rt(21.0, 21.0, settled=False, last_written=None)
    min_flow.sync(rt)
    rt.override.write_min_flow.assert_not_called()
