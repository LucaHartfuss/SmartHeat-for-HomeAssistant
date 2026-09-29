from types import SimpleNamespace
from unittest.mock import MagicMock

from heizungsbruecke import min_flow

OPTIONS = {"min_flow_min": 20.0, "min_flow_max": 30.0}


def _rt(target, live):
    ha_api = MagicMock()
    ha_api.get_state.side_effect = lambda ref: live() if callable(live) else live
    override = MagicMock()
    override.write_min_flow.side_effect = lambda value: value
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
