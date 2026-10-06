import json

import pytest

from smartheat_gateway.target_store import SOURCE_PORTAL, SOURCE_THERMOSTAT, TargetStore, is_valid_portal_target


@pytest.mark.parametrize(("value", "ok"), [
    (15.0, True), (25.0, True), (21.5, True), (14.5, False), (25.5, False), (21.3, False), (True, False),
    (float("nan"), False), ("21", False),
])
def test_portal_range_and_step(value, ok):
    assert is_valid_portal_target(value) is ok


def _store(tmp_path, clock, start=20.0, changes=None):
    return TargetStore(tmp_path / "runtime" / "room_target.json", start, clock, lambda: "2026-01-15T08:00:00+01:00",
                       on_change=(lambda: changes.append(1)) if changes is not None else None)


def test_start_value_is_written_once(tmp_path, clock):
    store = _store(tmp_path, clock)
    assert (store.value, store.source) == (20.0, SOURCE_PORTAL)
    assert json.loads((tmp_path / "runtime" / "room_target.json").read_text())["value"] == 20.0
    assert _store(tmp_path, clock, start=22.0).value == 20.0  # vorhandener Wert gilt


def test_last_set_value_wins(tmp_path, clock):
    changes = []
    store = _store(tmp_path, clock, changes=changes)
    assert store.apply_portal(22.0)
    clock.advance(61)
    assert store.apply_thermostat(19.0)
    assert (store.value, store.source) == (19.0, SOURCE_THERMOSTAT)
    assert store.apply_portal(21.0)
    assert len(changes) == 3


def test_portal_values_outside_the_range_are_rejected(tmp_path, clock):
    store = _store(tmp_path, clock)
    assert not store.apply_portal(26.0)
    assert not store.apply_portal(20.3)
    assert store.value == 20.0


def test_echo_window_and_thermostat_rounding(tmp_path, clock):
    store = _store(tmp_path, clock)
    store.apply_portal(21.5)
    store.note_own_write()
    clock.advance(30)
    assert not store.apply_thermostat(21.5)   # Echo
    assert not store.apply_thermostat(21.0)   # im Fenster: auch ein abweichender Wert ist Echo
    clock.advance(31)
    assert store.apply_thermostat(21.0)       # danach: Thermostat rundet anders, sein Wert gilt
    assert store.value == 21.0


def test_thermostat_report_of_the_current_value_is_a_no_op(tmp_path, clock):
    changes = []
    store = _store(tmp_path, clock, changes=changes)
    assert store.apply_portal(21.5)
    store.note_own_write()
    clock.advance(61)
    assert not store.apply_thermostat(21.5)  # Echo nach dem Fenster: kein Wechsel der Quelle, keine Meldung
    assert (store.value, store.source, len(changes)) == (21.5, SOURCE_PORTAL, 1)


def test_thermostat_plausibility_without_clamp(tmp_path, clock):
    store = _store(tmp_path, clock)
    assert store.apply_thermostat(30.0)        # ausserhalb 15-25, aber plausibel: gilt (kein Clamp)
    assert not store.apply_thermostat(36.0)    # unplausibel: verworfen
    assert store.value == 30.0
