"""Mindestvorlauf = Raum-Soll (derived.py) ueber die echte Hebel-Pipeline und das HA-Binding auf Fake-HA (A5-25): die
Zusicherungen stehen an den Schreibvorgaengen in HA und am Zustand (min_flow_current)."""
import dataclasses
import logging
from types import SimpleNamespace

import pytest
from bindung_beispiele import VAILLANT_MYPYLLANT, WEISHAUPT_MODBUS
from fakes import FREMDTEXT, FREMDTEXT_IM_LOG, FakeHa, logzeilen

from heizungsbruecke.ha_binding import HaPlantBinding
from smartheat_core import derived
from smartheat_core.pipeline import LeverPipeline
from smartheat_core.safety import LocalSafety
from smartheat_runtime.roles import ChannelManifest

# Bereich des Mindestvorlaufs wie bisher (min_flow_min/min_flow_max 20/30), jetzt aus LocalSafety.
SAFETY = LocalSafety(
    ranges={"curve": (0.4, 1.5), "room_setpoint": (15.0, 25.0), "heat_limit": (5.0, 23.0), "min_flow": (20.0, 30.0)},
    comfort_boost={}, emergency_boost_levers=(), arrival_threshold_k=0.5,
)
MANIFEST = ChannelManifest(refs={"min_flow": "number.min_flow"})
SETTLE = VAILLANT_MYPYLLANT.settle_seconds


class _LesefehlerHa(FakeHa):
    """Das Lesen des Mindestvorlaufs scheitert (z. B. Zeitueberschreitung der HA-API), Schreiben geht."""

    def get_state(self, entity_id):
        if entity_id == "number.min_flow":
            raise OSError("Zeitueberschreitung")
        return super().get_state(entity_id)


def _rt(make_store, clock, target, live, *, settled=True, ha=None, safety=SAFETY):
    """Laufzeit-Sicht von derived (store, override) mit echtem StateStore und echter LeverPipeline; `live` ist der
    Mindestvorlauf in HA (eine Exception wird beim Lesen geworfen). settled=False: kurz nach dem Start, die Pipeline
    hat noch nicht eingeschwungen."""
    store = make_store()
    store.update(stable_target=target)
    ha = ha or FakeHa()
    ha.states["number.min_flow"] = live
    pipeline = LeverPipeline(store, HaPlantBinding(ha, MANIFEST, VAILLANT_MYPYLLANT), safety, clock=clock)
    if settled:
        clock.advance(SETTLE + 1)
    return SimpleNamespace(store=store, override=pipeline, ha=ha)


def test_expected_is_room_target_clamped_and_rounded(make_store, clock):
    assert derived.expected(_rt(make_store, clock, 20.46, 20.0)) == 20.5
    assert derived.expected(_rt(make_store, clock, 18.0, 20.0)) == 20.0
    assert derived.expected(_rt(make_store, clock, None, 20.0)) is None


def test_erwarteter_mindestvorlauf_bleibt_in_den_grenzen(make_store, clock):
    """A6-32: untere Grenze neben dem Raster (20,25 bei Schritt 0,1): der erwartete Wert ist die Grenze wie geschrieben,
    nicht der auf 20,2 gerundete Wert unter ihr; die Anlage auf 20,25 gilt als in Ordnung, es wird nichts geschrieben."""
    sicherheit = dataclasses.replace(SAFETY, ranges={**SAFETY.ranges, "min_flow": (20.25, 30.0)})
    rt = _rt(make_store, clock, 20.0, 20.25, safety=sicherheit)
    assert VAILLANT_MYPYLLANT.steps["min_flow"] == 0.1
    assert derived.expected(rt) == 20.25
    derived.sync(rt)
    assert rt.ha.writes == []
    assert rt.store.state.min_flow_current == 20.25


@pytest.mark.parametrize("live", [21.0, 20.95, 21.05], ids=["gleich", "0,05-darunter", "0,05-darueber"])
def test_sync_writes_nothing_within_the_tolerance_of_0_05_k(make_store, clock, live):
    rt = _rt(make_store, clock, 21.0, live)
    derived.sync(rt)
    assert rt.ha.writes == []
    assert rt.store.state.min_flow_current == 21.0


@pytest.mark.parametrize("live", [20.94, 21.06], ids=["0,06-darunter", "0,06-darueber"])
def test_sync_writes_a_deviation_just_above_0_05_k(make_store, clock, live):
    rt = _rt(make_store, clock, 21.0, live)
    assert rt.store.state.min_flow_current is None
    derived.sync(rt)
    assert rt.ha.writes == [("number.min_flow", 21.0)]
    assert rt.store.state.min_flow_current == 21.0


def test_sync_writes_when_live_value_unreadable(make_store, clock):
    rt = _rt(make_store, clock, 21.0, 21.0, ha=_LesefehlerHa())
    derived.sync(rt)
    assert rt.ha.writes == [("number.min_flow", 21.0)]
    assert rt.store.state.min_flow_current == 21.0


def test_sync_never_raises_and_keeps_the_state_when_the_write_fails(make_store, clock, caplog):
    rt = _rt(make_store, clock, 21.0, 20.0)
    rt.ha.write_error = RuntimeError("cloud")
    with caplog.at_level(logging.ERROR, logger="smartheat_core.derived"):
        derived.sync(rt)  # wirft nicht
    assert rt.ha.writes == [] and rt.store.state.min_flow_current is None
    assert "Mindestvorlauf konnte nicht geschrieben werden" in caplog.text


def test_unsettled_sync_trusts_the_own_last_write_not_the_lagging_ha_value(make_store, clock):
    rt = _rt(make_store, clock, 22.0, 21.0, settled=False)
    rt.override.write_lever("min_flow", 22.0)
    rt.ha.states["number.min_flow"] = 21.0  # HA zeigt noch den alten Wert
    rt.ha.writes.clear()
    derived.sync(rt)
    assert rt.ha.writes == []
    assert rt.store.state.min_flow_current == 22.0


def test_unsettled_sync_writes_a_change_back_even_if_ha_still_shows_it(make_store, clock):
    rt = _rt(make_store, clock, 21.0, 21.0, settled=False)
    rt.override.write_lever("min_flow", 22.0)
    rt.ha.states["number.min_flow"] = 21.0  # HA zeigt noch den Wert vor dem eigenen Schreiben
    rt.ha.writes.clear()
    derived.sync(rt)
    assert rt.ha.writes == [("number.min_flow", 21.0)]


def test_unsettled_sync_without_own_write_since_start_compares_with_ha(make_store, clock):
    rt = _rt(make_store, clock, 21.0, 21.0, settled=False)
    derived.sync(rt)
    assert rt.ha.writes == []
    assert rt.store.state.min_flow_current == 21.0


def test_lever_sets_without_min_flow_derive_nothing(make_store, clock):
    """Plan 3b: Weishaupt/Viessmann haben keinen Mindestvorlauf (kein min_flow in client_derived, kein Bereich)."""
    store = make_store()
    store.update(stable_target=20.5)
    ha = FakeHa()
    no_min_flow = LocalSafety(ranges={}, comfort_boost={}, emergency_boost_levers=(), arrival_threshold_k=0.5)
    rt = SimpleNamespace(store=store, override=LeverPipeline(
        store, HaPlantBinding(ha, MANIFEST, WEISHAUPT_MODBUS), no_min_flow, clock=clock))
    assert derived.expected(rt) is None
    derived.sync(rt)
    assert ha.writes == [] and store.state.min_flow_current is None


class _FremdtextHa(FakeHa):
    """Das Lesen des Mindestvorlaufs scheitert mit fremdem Text (Fehlertext der HA-API)."""

    def get_state(self, entity_id):
        if entity_id == "number.min_flow":
            raise RuntimeError(FREMDTEXT)
        return super().get_state(entity_id)


def test_sync_logs_a_read_error_from_ha_only_escaped(make_store, clock, caplog):
    """T6-Rest, R-c: der Fehlertext beim Lesen des Mindestvorlaufs steht nur als Escape im Log."""
    rt = _rt(make_store, clock, 21.0, 21.0, ha=_FremdtextHa())
    with caplog.at_level(logging.WARNING, logger="smartheat_core.derived"):
        derived.sync(rt)
    assert f"Mindestvorlauf nicht lesbar ({FREMDTEXT_IM_LOG}), wird neu geschrieben" in logzeilen(caplog)
