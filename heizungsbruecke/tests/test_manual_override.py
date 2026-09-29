"""Durchsetzung (TP11, Spec 5.3)."""
from datetime import date
from unittest.mock import MagicMock

import pytest

from heizungsbruecke import manual_override
from heizungsbruecke.delivery import SOURCE_LOCAL, DataFault, DeliveryState
from heizungsbruecke.manifest import ChannelManifest
from heizungsbruecke.notifier import STATE_OK, Notifier
from heizungsbruecke.override import OWN_WRITE_SETTLE_SECONDS, Override
from heizungsbruecke.runtime import Runtime

OPTIONS = {
    "curve_min": 0.4, "curve_max": 1.5, "shift_min": 15.0, "shift_max": 25.0,
    "min_flow_min": 20.0, "min_flow_max": 30.0, "boost_curve_value": 1.5, "boost_shift_value": 25.0,
}
MANIFEST = ChannelManifest(entity_ids={
    "curve_current": "number.curve", "shift_current": "climate.zone::temperature", "min_flow": "number.mf",
})
POINT = {"curve_current": 0.9, "shift_current": 21.0}
TODAY = date(2026, 10, 3)


class Ha:
    def __init__(self, curve=0.9, shift=21.0, min_flow=20.5, mode="heat_cool", reflects_writes=True):
        self.states = {"number.curve": curve, "climate.zone::temperature": shift, "number.mf": min_flow,
                       "climate.zone": mode}
        self.writes = []
        # False: mypyllant uebernimmt das Rueckschreiben nicht (oder jemand stellt sofort wieder
        # um) -- HA zeigt weiter den verstellten Wert.
        self.reflects_writes = reflects_writes

    def get_state(self, ref):
        return self.states[ref]

    def get_raw_state(self, entity_id):
        return self.states[entity_id]

    def _set(self, key, value):
        if self.reflects_writes:
            self.states[key] = value

    def set_number_value(self, entity_id, value):
        self.writes.append((entity_id, value))
        self._set(entity_id, value)

    def set_climate_temperature(self, entity_id, value):
        self.writes.append((entity_id, value))
        self._set(f"{entity_id}::temperature", value)

    def set_hvac_mode(self, entity_id, mode):
        self.writes.append((entity_id, mode))
        self._set(entity_id, mode)


@pytest.fixture(autouse=True)
def _fixed_day(monkeypatch):
    # Kein Flackern um Mitternacht: der Tageszaehler haengt an _today().
    monkeypatch.setattr(manual_override, "_today", lambda: TODAY)


def _rt(make_store, clock, ha, **backup):
    store = make_store(backup={**POINT, **backup})
    store.update(stable_target=20.5)
    notifier = MagicMock(spec=Notifier)
    notifier.notify.return_value = True
    return Runtime(
        manifest=MANIFEST, ha_api=ha, options=OPTIONS, derived_entity_ids={}, worker=MagicMock(), store=store,
        override=Override(store, MANIFEST, ha, OPTIONS, clock=clock), notifier=notifier, clock=clock,
    )


def _rounds(rt, n=manual_override.DETECTION_ROUNDS):
    for _ in range(n):
        manual_override.check_manual_override(rt)


def _curve_writes(rt):
    return [w for w in rt.ha_api.writes if w[0] == "number.curve"]


def _message_calls(rt):
    return [
        c for c in rt.notifier.notify.call_args_list
        if c.args[1] != STATE_OK and not str(c.args[1]).startswith("limit")
    ]


def test_settle_window_is_the_one_from_override():
    assert manual_override.OWN_WRITE_SETTLE_SECONDS is OWN_WRITE_SETTLE_SECONDS


def test_no_deviation_no_write(make_store, clock):
    rt = _rt(make_store, clock, Ha())
    _rounds(rt)
    assert rt.ha_api.writes == []


def test_manual_curve_change_is_written_back_and_reported(make_store, clock):
    rt = _rt(make_store, clock, Ha(curve=1.3))
    _rounds(rt)
    assert rt.ha_api.writes == [("number.curve", 0.9)]
    assert rt.store.state.manual_override_pending["curve"] == 1.3
    assert rt.notifier.notify.call_args.args[0] == manual_override.KEY


def test_detection_needs_two_rounds(make_store, clock):
    rt = _rt(make_store, clock, Ha(curve=1.3))
    _rounds(rt, 1)
    assert rt.ha_api.writes == []


def test_zone_mode_is_restored_and_shift_rewritten(make_store, clock):
    rt = _rt(make_store, clock, Ha(mode="auto"))
    _rounds(rt)
    assert ("climate.zone", "heat_cool") in rt.ha_api.writes
    assert ("climate.zone", 21.0) in rt.ha_api.writes


def test_zone_mode_restore_switches_the_mode_exactly_once(make_store, clock):
    # Ruling #3: write_roles schaltet die Zone selbst um, kein zweites set_hvac_mode davor.
    # Zone und Wert verstellt, HA hinkt nach: Betriebsart und Parallelverschiebung sind EIN
    # Rueckschreiben (sonst schaltete der zweite Schreibvorgang auf dem veralteten "auto" erneut).
    rt = _rt(make_store, clock, Ha(mode="auto", shift=18.0, reflects_writes=False))
    _rounds(rt)
    assert [w for w in rt.ha_api.writes if w[1] == "heat_cool"] == [("climate.zone", "heat_cool")]
    assert [w for w in rt.ha_api.writes if w[1] == 21.0] == [("climate.zone", 21.0)]


def test_zone_mode_restore_does_not_switch_again_while_ha_lags(make_store, clock):
    # HA zeigt nach dem Umschalten noch bis zu 30 min "auto" (mypyllant): keine weitere Umschaltung
    # in dieser Zeit, und der Hinweis bleibt stehen (kein "wieder in Ordnung").
    rt = _rt(make_store, clock, Ha(mode="auto", reflects_writes=False))
    _rounds(rt)
    for _ in range(5):
        clock.advance(300)
        _rounds(rt)
    assert [w for w in rt.ha_api.writes if w[1] == "heat_cool"] == [("climate.zone", "heat_cool")]
    assert rt.store.state.manual_override is not None
    assert [c for c in rt.notifier.notify.call_args_list if c.args[1] == STATE_OK] == []


def test_min_flow_deviation_is_written_back(make_store, clock):
    rt = _rt(make_store, clock, Ha(min_flow=25.0))
    _rounds(rt)
    assert rt.ha_api.writes == [("number.mf", 20.5)]


def test_zone_zero_is_no_deviation(make_store, clock):
    rt = _rt(make_store, clock, Ha(shift=0.0))
    _rounds(rt)
    assert rt.ha_api.writes == []
    assert rt.store.state.manual_override is None


def test_pause_after_own_write(make_store, clock):
    rt = _rt(make_store, clock, Ha(curve=1.3))
    rt.override.write_roles(("curve_current",))  # eigener Schreibvorgang
    rt.ha_api.states["number.curve"] = 1.3  # HA zeigt noch den alten Wert
    rt.ha_api.writes.clear()
    _rounds(rt)
    assert rt.ha_api.writes == []


def test_enforcement_rate_limited_per_day(make_store, clock):
    # Review Focus 2: mypyllant uebernimmt das Rueckschreiben nicht -- hoechstens
    # MAX_WRITES_PER_DAY Schreibvorgaenge, genau eine Eingriffs- und eine Limit-Meldung.
    rt = _rt(make_store, clock, Ha(curve=1.3, reflects_writes=False))
    for _ in range(20):
        clock.advance(manual_override.OWN_WRITE_SETTLE_SECONDS + 1)
        _rounds(rt)
    assert len(_curve_writes(rt)) == manual_override.MAX_WRITES_PER_DAY
    assert len(_message_calls(rt)) == 1
    limit_calls = [c for c in rt.notifier.notify.call_args_list if str(c.args[1]).startswith("limit")]
    assert len(limit_calls) == 1
    assert [c for c in rt.notifier.notify.call_args_list if c.args[1] == STATE_OK] == []


def test_ignored_write_is_reported_once_until_the_return(make_store, clock):
    # Plan-Praezisierung "Durchsetzungs-Meldung": die Meldung bleibt stehen, bis der Istwert
    # wieder stimmt; erst danach ist ein neuer Eingriff eine neue Meldung.
    rt = _rt(make_store, clock, Ha(curve=1.3, reflects_writes=False))
    _rounds(rt)
    for _ in range(3):
        clock.advance(manual_override.OWN_WRITE_SETTLE_SECONDS + 1)
        _rounds(rt)
    assert len(_message_calls(rt)) == 1
    rt.ha_api.states["number.curve"] = 0.9  # die Anlage uebernimmt den Wert endlich
    clock.advance(manual_override.OWN_WRITE_SETTLE_SECONDS + 1)
    _rounds(rt)
    assert rt.store.state.manual_override is None
    assert rt.notifier.notify.call_args.args[1] == STATE_OK


def test_retry_interval_between_writes(make_store, clock):
    rt = _rt(make_store, clock, Ha(curve=1.3))
    _rounds(rt)
    rt.ha_api.states["number.curve"] = 1.3
    clock.advance(manual_override.OWN_WRITE_SETTLE_SECONDS + 1)  # 35 min > 30 min
    _rounds(rt)
    assert len(_curve_writes(rt)) == 2


@pytest.mark.parametrize(("ago", "allowed"), [(manual_override.RETRY_SECONDS - 1, False),
                                              (manual_override.RETRY_SECONDS + 1, True)])
def test_may_write_respects_the_retry_interval(make_store, clock, ago, allowed):
    # Ruling #9: RETRY_SECONDS direkt pruefen (im Ablauf wird es von der Schonfrist verdeckt).
    rt = _rt(make_store, clock, Ha())
    rt.store.update(enforce_log={"curve_current": {"day": TODAY.isoformat(), "count": 1, "last": clock() - ago}})
    assert manual_override._may_write(rt, "curve_current") is allowed


def test_may_write_respects_the_daily_limit(make_store, clock):
    rt = _rt(make_store, clock, Ha())
    day = TODAY.isoformat()
    rt.store.update(enforce_log={"curve_current": {"day": day, "count": manual_override.MAX_WRITES_PER_DAY,
                                                   "last": clock() - 10 * manual_override.RETRY_SECONDS}})
    assert manual_override._may_write(rt, "curve_current") is False
    assert manual_override._may_write(rt, "shift_current") is True


def test_open_data_fault_pauses(make_store, clock):
    rt = _rt(make_store, clock, Ha(curve=1.3))
    rt.store.set_delivery(DeliveryState(datenfehler=DataFault(SOURCE_LOCAL, ("room_actual",))))
    _rounds(rt)
    assert rt.ha_api.writes == []


def test_return_clears_hint(make_store, clock):
    rt = _rt(make_store, clock, Ha(curve=1.3))
    _rounds(rt)
    manual_override.check_manual_override(rt)  # Anlage steht wieder richtig
    assert rt.store.state.manual_override is None
    assert rt.notifier.notify.call_args.args[1] == STATE_OK


def test_counter_resets_next_day(make_store, clock, monkeypatch):
    rt = _rt(make_store, clock, Ha(curve=1.3))
    monkeypatch.setattr(manual_override, "_today", lambda: date(2026, 10, 3))
    for _ in range(10):
        rt.ha_api.states["number.curve"] = 1.3
        clock.advance(manual_override.OWN_WRITE_SETTLE_SECONDS + 1)
        _rounds(rt)
    assert len(_curve_writes(rt)) == manual_override.MAX_WRITES_PER_DAY
    monkeypatch.setattr(manual_override, "_today", lambda: date(2026, 10, 4))
    rt.ha_api.states["number.curve"] = 1.3
    clock.advance(manual_override.OWN_WRITE_SETTLE_SECONDS + 1)
    before = len(rt.ha_api.writes)
    _rounds(rt)
    assert len(rt.ha_api.writes) == before + 1


@pytest.mark.parametrize("flag", ["boost_active", "emergency_boost_active"])
def test_boost_row_is_the_target_during_boost(make_store, clock, flag):
    rt = _rt(make_store, clock, Ha(curve=1.5, shift=25.0))
    rt.store.update(**{flag: True})
    _rounds(rt)
    assert rt.ha_api.writes == []
