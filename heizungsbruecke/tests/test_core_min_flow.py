"""Mindestvorlauf als Serverhebel ueber die echte Hebel-Pipeline und das HA-Binding auf Fake-HA: der Server sendet
`min_flow`, das Geraet klemmt ihn auf seine lokalen Grenzen und schreibt ihn wie jeden Hebel. Die Zusicherungen stehen
an den Schreibvorgaengen in HA und am Wiederherstellungspunkt."""
import dataclasses
import logging

import pytest
from bindung_beispiele import VAILLANT_MYPYLLANT
from fakes import FakeHa

from heizungsbruecke.ha_binding import HaPlantBinding
from smartheat_core.pipeline import LeverPipeline
from smartheat_core.safety import LocalSafety
from smartheat_runtime.roles import ChannelManifest

# Bereich des Mindestvorlaufs (min_flow_min/min_flow_max 20/30) aus LocalSafety.
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


def _pipeline(make_store, clock, live, *, ha=None, safety=SAFETY, binding=HaPlantBinding):
    """Echter StateStore und echte LeverPipeline; `live` ist der Mindestvorlauf in HA."""
    store = make_store()
    ha = ha or FakeHa()
    ha.states["number.min_flow"] = live
    pipeline = LeverPipeline(store, binding(ha, MANIFEST, VAILLANT_MYPYLLANT), safety, clock=clock)
    clock.advance(SETTLE + 1)
    return store, ha, pipeline


@pytest.mark.parametrize(("server", "written"), [
    (20.46, 20.5), (20.5, 20.5), (21.0, 21.0), (20.05, 20.0), (29.96, 30.0), (24.99, 25.0),
], ids=lambda value: str(value))
def test_the_server_value_is_rounded_to_the_step_and_written(make_store, clock, server, written):
    """Gleicher Eingang wie frueher am Geraet (Raum-Soll gerundet auf 0,1 K): gleicher geschriebener Wert."""
    store, ha, pipeline = _pipeline(make_store, clock, 27.0)
    pipeline.apply_server_values({"min_flow": server})
    assert ha.writes == [("number.min_flow", written)]
    assert store.state.restore_point["min_flow"] == server


@pytest.mark.parametrize(("server", "written"), [(35.0, 30.0), (10.0, 20.0), (30.04, 30.0), (19.96, 20.0)])
def test_an_inadmissible_server_value_is_clamped_to_the_local_limit(make_store, clock, server, written, caplog):
    store, ha, pipeline = _pipeline(make_store, clock, 25.0)
    with caplog.at_level(logging.WARNING, logger="smartheat_core.pipeline"):
        pipeline.apply_server_values({"min_flow": server})
    assert ha.writes == [("number.min_flow", written)]
    assert store.state.restore_point["min_flow"] == written
    assert "Wert fuer Hebel 'min_flow' geclampt" in caplog.text


def test_a_narrowed_device_range_limits_the_server_value_further(make_store, clock):
    class Engeres(HaPlantBinding):
        def limits(self, lever):
            return (20.0, 24.0) if lever == "min_flow" else None

    store, ha, pipeline = _pipeline(make_store, clock, 21.0, binding=Engeres)
    pipeline.apply_server_values({"min_flow": 28.0})
    assert ha.writes == [("number.min_flow", 24.0)]
    assert pipeline.expected_values() == {"min_flow": 24.0}


def test_the_lower_limit_beside_the_grid_is_written_as_the_limit(make_store, clock):
    """Untere Grenze neben dem Raster (20,25 bei Schritt 0,1): geschrieben wird die Grenze, nicht der auf 20,2
    gerundete Wert unter ihr; die Anlage auf 20,25 gilt als in Ordnung, es wird nichts geschrieben."""
    sicherheit = dataclasses.replace(SAFETY, ranges={**SAFETY.ranges, "min_flow": (20.25, 30.0)})
    store, ha, pipeline = _pipeline(make_store, clock, 20.25, safety=sicherheit)
    assert VAILLANT_MYPYLLANT.steps["min_flow"] == 0.1
    pipeline.apply_server_values({"min_flow": 20.0})
    assert ha.writes == []
    assert pipeline.expected_values() == {"min_flow": 20.25}


@pytest.mark.parametrize("live", [21.0, 20.96, 21.04], ids=["gleich", "0,04-darunter", "0,04-darueber"])
def test_nothing_is_written_within_half_a_step(make_store, clock, live):
    store, ha, pipeline = _pipeline(make_store, clock, live)
    pipeline.apply_server_values({"min_flow": 21.0})
    assert ha.writes == []
    assert store.state.restore_point["min_flow"] == 21.0


@pytest.mark.parametrize("live", [20.94, 21.06], ids=["0,06-darunter", "0,06-darueber"])
def test_a_deviation_above_half_a_step_is_written(make_store, clock, live):
    store, ha, pipeline = _pipeline(make_store, clock, live)
    pipeline.apply_server_values({"min_flow": 21.0})
    assert ha.writes == [("number.min_flow", 21.0)]


def test_it_is_written_when_the_live_value_is_unreadable(make_store, clock):
    store, ha, pipeline = _pipeline(make_store, clock, 21.0, ha=_LesefehlerHa())
    pipeline.apply_server_values({"min_flow": 21.0})
    assert ha.writes == [("number.min_flow", 21.0)]


def test_nothing_is_written_without_a_server_value(make_store, clock):
    """Ohne Serverwert (und ohne Wiederherstellungspunkt) schreibt das Geraet keinen Mindestvorlauf selbst."""
    store, ha, pipeline = _pipeline(make_store, clock, 25.0)
    pipeline.apply_server_values({})
    assert ha.writes == []
    assert "min_flow" not in store.state.restore_point


def test_the_value_is_kept_for_the_enforcement_and_the_status(make_store, clock):
    store, ha, pipeline = _pipeline(make_store, clock, 25.0)
    pipeline.apply_server_values({"min_flow": 22.04})
    assert pipeline.expected_values() == {"min_flow": 22.0}
    assert store.state.restore_point == {"min_flow": 22.04}
