"""Manueller Eingriff an Kurve/Offset (R6, Spec TP7 3.6)."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from heizungsbruecke import manual_override
from heizungsbruecke.delivery import SOURCE_LOCAL, SOURCE_WRITE, DataFault, DeliveryState
from heizungsbruecke.manifest import ChannelManifest
from heizungsbruecke.notifier import STATE_OK, Notifier

MANIFEST = ChannelManifest(entity_ids={"curve_current": "number.curve", "offset_current": "number.offset"})
POINT = {"curve_current": 0.9, "offset_current": 22.0}


def _rt(make_store, live=(0.9, 22.0), hints_off=(), **backup):
    store = make_store(backup={**POINT, **backup})
    ha_api = MagicMock()
    values = {"number.curve": live[0], "number.offset": live[1]}

    def _get_state(entity_id):
        value = values[entity_id]
        if isinstance(value, Exception):
            raise value
        return value

    ha_api.get_state.side_effect = _get_state
    return SimpleNamespace(
        store=store, ha_api=ha_api, manifest=MANIFEST, values=values,
        notifier=Notifier(store, ha_api, ["notify.handy"], hints_off),
    )


def _rounds(rt, count=1):
    for _ in range(count):
        manual_override.check_manual_override(rt)


def test_deviation_is_reported_only_after_two_rounds(make_store):
    rt = _rt(make_store, live=(1.3, 24.5))

    _rounds(rt)
    assert rt.store.state.manual_override is None
    rt.ha_api.send_notification.assert_not_called()

    _rounds(rt)
    detected = rt.store.state.manual_override
    assert (detected["curve"], detected["offset"]) == (1.3, 24.5)
    assert rt.store.state.manual_override_pending == detected
    assert "manuell auf 1.3 / Offset 24.5 gestellt" in rt.ha_api.send_notification.call_args.args[1]
    assert rt.notifier.state("manueller_eingriff") == "1.3/24.5"


def test_the_same_deviation_is_reported_once(make_store):
    rt = _rt(make_store, live=(1.3, 24.5))

    _rounds(rt, 5)

    assert rt.ha_api.send_notification.call_count == 1


def test_a_changed_manual_value_is_reported_again(make_store):
    rt = _rt(make_store, live=(1.3, 24.5))
    _rounds(rt, 2)

    rt.values["number.curve"] = 1.4
    _rounds(rt)

    assert rt.ha_api.send_notification.call_count == 2
    assert rt.store.state.manual_override["curve"] == 1.4


@pytest.mark.parametrize("live", [(0.91, 22.0), (0.9, 22.1), (0.89, 21.9)])
def test_deviation_within_the_tolerance_is_ignored(make_store, live):
    rt = _rt(make_store, live=live)

    _rounds(rt, 3)

    assert rt.store.state.manual_override is None


@pytest.mark.parametrize("backup", [{"boost_active": True}, {"emergency_boost_active": True}])
def test_no_detection_during_a_boost(make_store, backup):
    rt = _rt(make_store, live=(1.5, 30.0), **backup)

    _rounds(rt, 3)

    assert rt.store.state.manual_override is None


def test_no_detection_during_a_write_fault(make_store):
    rt = _rt(make_store, live=(1.3, 24.5))
    rt.store.set_delivery(DeliveryState(datenfehler=DataFault(SOURCE_WRITE, ("curve_current (number.curve): weg",))))

    _rounds(rt, 3)

    assert rt.store.state.manual_override is None


def test_no_detection_after_a_write_fault_turns_into_a_read_fault(make_store):
    """Fix Runde 1, Befund 1: ein Schreibfehler kann durch einen anderen Datenfehler abgeloest
    werden (delivery._read_invalid/_ack bei einer Ablehnung), waehrend die Anlage noch auf den
    alten Werten steht -- die Erkennung muss bei jedem offenen Datenfehler pausieren, nicht nur
    bei einem Schreibfehler."""
    rt = _rt(make_store, live=(1.3, 24.5))
    rt.store.set_delivery(DeliveryState(datenfehler=DataFault(SOURCE_WRITE, ("curve_current (number.curve): weg",))))
    _rounds(rt)  # noch Schreibfehler

    rt.store.set_delivery(DeliveryState(datenfehler=DataFault(SOURCE_LOCAL, ("room_actual",))))
    _rounds(rt, 2)  # zwei Runden mit einem anderen (nicht Schreib-) Datenfehler

    assert rt.store.state.manual_override is None


def test_no_detection_without_a_restore_point(make_store):
    rt = _rt(make_store, live=(1.3, 24.5), curve_current=None)

    _rounds(rt, 3)

    assert rt.store.state.manual_override is None


def test_an_unreadable_live_value_is_no_deviation_and_restarts_the_count(make_store):
    rt = _rt(make_store, live=(1.3, 24.5))
    _rounds(rt)
    rt.values["number.curve"] = RuntimeError("unavailable")
    _rounds(rt)
    rt.values["number.curve"] = 1.3

    _rounds(rt)
    assert rt.store.state.manual_override is None
    _rounds(rt)
    assert rt.store.state.manual_override is not None


def test_lagging_state_after_own_write_is_not_a_manual_override(make_store):
    """Review Focus 5: direkt nach dem Schreiben neuer Serverwerte zeigt HA fuer eine Runde noch
    die alten Werte."""
    rt = _rt(make_store, live=(0.9, 22.0))
    rt.store.update(curve_current=0.95, offset_current=23.0)

    _rounds(rt)  # HA hinkt nach
    rt.values.update({"number.curve": 0.95, "number.offset": 23.0})
    _rounds(rt, 2)

    assert rt.store.state.manual_override is None
    rt.ha_api.send_notification.assert_not_called()


def test_return_to_the_learned_values_clears_the_hint_silently(make_store):
    rt = _rt(make_store, live=(1.3, 24.5))
    _rounds(rt, 2)
    rt.ha_api.reset_mock()

    rt.values.update({"number.curve": 0.9, "number.offset": 22.0})
    _rounds(rt)

    assert rt.store.state.manual_override is None
    assert rt.store.state.manual_override_pending is not None  # KPI bleibt, bis der Server es hat
    assert rt.notifier.state("manueller_eingriff") == STATE_OK
    rt.ha_api.send_notification.assert_not_called()


def test_switched_off_hint_is_detected_but_not_pushed(make_store):
    rt = _rt(make_store, live=(1.3, 24.5), hints_off=["manueller_eingriff"])

    _rounds(rt, 2)

    assert rt.store.state.manual_override is not None
    rt.ha_api.send_notification.assert_not_called()
