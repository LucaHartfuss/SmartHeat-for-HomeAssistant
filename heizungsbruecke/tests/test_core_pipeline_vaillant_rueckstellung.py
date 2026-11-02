"""Hebel-Pipeline bei Vaillant: Abo-Ende, Abmelden und Werksreset (beide laufen ueber das Abmelden) stellen die vier
Hebel (Steigung, Parallelverschiebung, Heizgrenze, Mindestvorlauf) auf ihre Ursprungswerte und die Betriebsart der Zone
zuletzt auf das Zeitprogramm zurueck. Die Attrappe kennt das Quick-Veto: ein Sollwert, der bei einer Zone ausserhalb von
Manuell geschrieben wird, gilt als Veto und wird gezaehlt."""
import logging

import pytest
from bindung_beispiele import VAILLANT_MYPYLLANT

from heizungsbruecke.ha_binding import MANUAL_HVAC_MODE, HaPlantBinding
from smartheat_core.pipeline import DeviceWriteError, LeverPipeline
from smartheat_core.safety import LocalSafety
from smartheat_runtime.roles import ChannelManifest

ZONE = "climate.zone"
ZONE_REF = "climate.zone::temperature"
SAFETY = LocalSafety(
    ranges={"curve": (0.4, 1.5), "room_setpoint": (15.0, 25.0), "heat_limit": (5.0, 20.0), "min_flow": (20.0, 30.0)},
    comfort_boost={"curve": 1.0, "room_setpoint": 24.0, "heat_limit": 20.0},
    emergency_boost_levers=("curve", "room_setpoint", "heat_limit"),
    arrival_threshold_k=0.5,
)
MANIFEST = ChannelManifest(refs={
    "curve_current": "number.curve", "shift_current": ZONE_REF, "heat_limit": "number.grenze",
    "min_flow": "number.mindestvorlauf",
})
# Werte der Anlage vor SmartHeat (Ursprung) und die Werte, die der Server dann setzt.
ORIGINAL = {"number.curve": 0.7, ZONE_REF: 21.0, "number.grenze": 15.0, "number.mindestvorlauf": 22.0}
SERVER = {"curve": 0.9, "room_setpoint": 22.5, "heat_limit": 18.0, "min_flow": 25.0}
ENTITY_OF = {"curve": "number.curve", "room_setpoint": ZONE_REF, "heat_limit": "number.grenze",
             "min_flow": "number.mindestvorlauf"}
WEGE = [pytest.param(True, id="abo_ende"), pytest.param(False, id="abmelden_und_werksreset")]


class ZoneHa:
    """Zone mit Betriebsart; Sollwert-Schreiben ausserhalb von Manuell ist ein Quick-Veto (nicht wirksam)."""

    def __init__(self, mode="auto"):
        self.states = dict(ORIGINAL)
        self.modes = {ZONE: mode}
        self.events = []
        self.vetoes = []

    def get_state(self, ref):
        return self.states[ref]

    def get_raw_state(self, entity_id):
        if entity_id in self.modes:
            mode = self.modes[entity_id]
            if mode in ("unavailable", "unknown"):
                raise ValueError(f"Entity {entity_id} hat keinen gueltigen Zustand: {mode!r}")
            return mode
        return "on"

    def get_attribute(self, entity_id, name):
        raise KeyError(name)

    def set_number_value(self, entity_id, value):
        self.events.append(("write", entity_id, value))
        self.states[entity_id] = value

    def set_climate_temperature(self, entity_id, value):
        self.events.append(("write", ZONE_REF, value))
        if self.modes[entity_id] != MANUAL_HVAC_MODE:
            self.vetoes.append(value)
            return
        self.states[ZONE_REF] = value

    def set_hvac_mode(self, entity_id, mode):
        self.events.append(("mode", entity_id, mode))
        self.modes[entity_id] = mode

    @property
    def written(self):
        return [event[1] for event in self.events if event[0] == "write"]

    @property
    def mode_switches(self):
        return [event[2] for event in self.events if event[0] == "mode"]


def _setup(make_store, clock, mode="auto", server=None):
    store = make_store()
    ha = ZoneHa(mode)
    binding = HaPlantBinding(ha, MANIFEST, VAILLANT_MYPYLLANT)
    pipeline = LeverPipeline(store, binding, SAFETY, clock=clock,
                             server_originals=(lambda: server) if server is not None else None)
    clock.advance(VAILLANT_MYPYLLANT.settle_seconds + 1)
    return pipeline, store, ha


def _smartheat_laeuft(pipeline, clock, ha):
    """Der Server setzt seine Werte: Ursprung merken, Zone auf Manuell, vier Hebel schreiben."""
    pipeline.apply_server_values(SERVER)
    clock.advance(VAILLANT_MYPYLLANT.settle_seconds + 1)
    assert ha.modes[ZONE] == MANUAL_HVAC_MODE
    assert {lever: ha.states[ref] for lever, ref in ENTITY_OF.items()} == SERVER
    ha.events.clear()


def _ursprung(ha):
    return {ref: ha.states[ref] for ref in ORIGINAL}


@pytest.mark.parametrize("always_restore", WEGE)
def test_every_lever_and_the_mode_go_back_and_the_mode_comes_last(make_store, clock, always_restore):
    pipeline, store, ha = _setup(make_store, clock, mode="auto")
    _smartheat_laeuft(pipeline, clock, ha)
    assert store.state.aux_originals == {"mode_select": "auto"}

    assert pipeline.restore_and_clear(always_restore) is True

    assert _ursprung(ha) == ORIGINAL
    assert ha.modes[ZONE] == "auto"
    assert ha.vetoes == []
    assert sorted(ha.written) == sorted(ORIGINAL)
    assert ha.events[-1] == ("mode", ZONE, "auto")
    assert [event[0] for event in ha.events].count("mode") == 1
    assert store.state.aux_originals == {}


@pytest.mark.parametrize("always_restore", WEGE)
def test_a_customer_in_manual_mode_before_keeps_manual_and_gets_the_levers_back(make_store, clock, always_restore):
    pipeline, store, ha = _setup(make_store, clock, mode=MANUAL_HVAC_MODE)
    _smartheat_laeuft(pipeline, clock, ha)
    assert store.state.aux_originals == {"mode_select": MANUAL_HVAC_MODE}

    assert pipeline.restore_and_clear(always_restore) is True

    assert _ursprung(ha) == ORIGINAL
    assert ha.modes[ZONE] == MANUAL_HVAC_MODE
    assert ha.mode_switches == []


@pytest.mark.parametrize("always_restore", WEGE)
def test_a_zone_that_was_off_before_is_not_switched_back_by_us(make_store, clock, always_restore):
    pipeline, store, ha = _setup(make_store, clock, mode="off")
    _smartheat_laeuft(pipeline, clock, ha)
    assert store.state.aux_originals == {"mode_select": "off"}

    assert pipeline.restore_and_clear(always_restore) is True

    assert _ursprung(ha) == ORIGINAL
    assert ha.mode_switches == []


@pytest.mark.parametrize("always_restore", WEGE)
def test_an_unknown_original_mode_is_remembered_not_written_and_logged(make_store, clock, caplog, always_restore):
    pipeline, store, ha = _setup(make_store, clock, mode="dry")
    _smartheat_laeuft(pipeline, clock, ha)
    assert store.state.aux_originals == {"mode_select": "dry"}
    caplog.clear()
    caplog.set_level(logging.INFO)

    assert pipeline.restore_and_clear(always_restore) is True

    assert _ursprung(ha) == ORIGINAL
    assert ha.mode_switches == []
    assert any("dry" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("always_restore", WEGE)
def test_a_zone_the_customer_put_on_the_schedule_meanwhile_gets_no_setpoint_write(make_store, clock, always_restore):
    """Steht die Zone beim Zurueckstellen schon nicht auf Manuell, waere der Sollwert ein Quick-Veto: er entfaellt,
    die uebrigen Hebel und die Betriebsart laufen."""
    pipeline, store, ha = _setup(make_store, clock, mode="auto")
    _smartheat_laeuft(pipeline, clock, ha)
    ha.modes[ZONE] = "auto"

    assert pipeline.restore_and_clear(always_restore) is True

    assert ha.vetoes == []
    assert ZONE_REF not in ha.written
    assert sorted(ha.written) == ["number.curve", "number.grenze", "number.mindestvorlauf"]
    assert {ref: ha.states[ref] for ref in ORIGINAL if ref != ZONE_REF} == {
        ref: value for ref, value in ORIGINAL.items() if ref != ZONE_REF}
    assert ha.modes[ZONE] == "auto"
    assert ha.mode_switches == []
    assert store.state.aux_originals == {}


@pytest.mark.parametrize("always_restore", WEGE)
def test_a_zone_that_stands_elsewhere_is_set_back_to_the_schedule_without_a_veto(make_store, clock, always_restore):
    pipeline, store, ha = _setup(make_store, clock, mode="auto")
    _smartheat_laeuft(pipeline, clock, ha)
    ha.modes[ZONE] = "off"

    assert pipeline.restore_and_clear(always_restore) is True

    assert ha.vetoes == []
    assert ZONE_REF not in ha.written
    assert ha.modes[ZONE] == "auto"


def test_the_levers_stay_in_the_lever_order_and_the_mode_after_all_of_them(make_store, clock):
    pipeline, _, ha = _setup(make_store, clock, mode="auto")
    _smartheat_laeuft(pipeline, clock, ha)

    pipeline.restore_and_clear(True)

    assert [event[1] for event in ha.events if event[0] == "write"] == [
        ENTITY_OF[lever] for lever in VAILLANT_MYPYLLANT.lever_set.levers]
    assert [event[0] for event in ha.events] == ["write"] * 4 + ["mode"]


def test_a_running_boost_does_not_overwrite_min_flow_and_the_restore_brings_it_back(make_store, clock):
    pipeline, store, ha = _setup(make_store, clock, mode="auto")
    _smartheat_laeuft(pipeline, clock, ha)

    pipeline.set_boosts(comfort=True, emergency=False)
    clock.advance(VAILLANT_MYPYLLANT.settle_seconds + 1)

    assert "number.mindestvorlauf" not in ha.written
    assert ha.states["number.mindestvorlauf"] == SERVER["min_flow"]
    ha.events.clear()

    assert pipeline.restore_and_clear(False) is True

    assert _ursprung(ha) == ORIGINAL
    assert ha.modes[ZONE] == "auto"
    assert ha.vetoes == []
    assert store.state.boost_active is False


def test_a_second_call_after_the_restore_writes_nothing_more(make_store, clock):
    pipeline, _, ha = _setup(make_store, clock, mode="auto")
    _smartheat_laeuft(pipeline, clock, ha)
    pipeline.restore_and_clear(False)
    ha.events.clear()
    clock.advance(VAILLANT_MYPYLLANT.settle_seconds + 1)

    assert pipeline.restore_and_clear(False) is True

    assert ha.events == []


def test_the_original_mode_is_remembered_before_the_zone_is_switched(make_store, clock):
    """Vor der Vorbereitung (Zone auf Manuell) muss die Betriebsart gemerkt sein: sonst ginge der Ursprung verloren.
    Ist sie nicht lesbar, geschieht nichts."""
    pipeline, store, ha = _setup(make_store, clock, mode="unavailable")

    with pytest.raises(DeviceWriteError):
        pipeline.apply_server_values(SERVER)

    assert ha.events == [] and store.state.aux_originals == {}
    ha.modes[ZONE] = "auto"
    pipeline.apply_server_values(SERVER)
    assert store.state.aux_originals == {"mode_select": "auto"}
    assert ha.mode_switches == [MANUAL_HVAC_MODE]


def test_the_restore_does_not_switch_the_zone_to_manual_first(make_store, clock):
    """Ohne gemerkte Betriebsart bereitet die Wiederherstellung nicht vor (sonst bliebe die Umstellung fuer immer);
    ein Sollwert in einer Zone ausserhalb von Manuell entfaellt, die uebrigen Hebel gehen zurueck."""
    server = {**{"curve": 0.7, "room_setpoint": 21.0, "heat_limit": 15.0, "min_flow": 22.0}}
    pipeline, store, ha = _setup(make_store, clock, mode="auto", server=server)
    for ref, wert in (("number.curve", 0.9), ("number.grenze", 18.0), ("number.mindestvorlauf", 25.0)):
        ha.states[ref] = wert
    assert store.state.aux_originals == {}

    assert pipeline.restore_and_clear(True) is True

    assert ha.mode_switches == []
    assert ha.vetoes == []
    assert ZONE_REF not in ha.written
    assert ha.states["number.curve"] == 0.7 and ha.states["number.mindestvorlauf"] == 22.0


@pytest.mark.parametrize(("vom_server", "erwartet"), [("eco", None), ("dry", None), (3.0, None), ("auto", "auto")])
def test_a_mode_from_the_server_counts_only_if_the_binding_knows_it(make_store, clock, caplog, vom_server, erwartet):
    server = {"curve": 0.7, "room_setpoint": 21.0, "heat_limit": 15.0, "min_flow": 22.0, "mode_select": vom_server}
    pipeline, store, ha = _setup(make_store, clock, mode=MANUAL_HVAC_MODE, server=server)
    caplog.set_level(logging.INFO)

    assert pipeline.restore_and_clear(True) is True

    assert ha.mode_switches == ([] if erwartet is None else [erwartet])
    if erwartet is None:
        assert ha.modes[ZONE] == MANUAL_HVAC_MODE
        assert any("verworfen" in record.getMessage() for record in caplog.records)
    else:
        assert ha.modes[ZONE] == "auto"


def test_a_failing_mode_switch_keeps_the_restore_open(make_store, clock):
    pipeline, store, ha = _setup(make_store, clock, mode="auto")
    _smartheat_laeuft(pipeline, clock, ha)

    def kaputt(entity_id, mode):
        raise RuntimeError("myVAILLANT-Cloud nicht erreichbar")

    ha.set_hvac_mode = kaputt

    assert pipeline.restore_and_clear(False) is False
    assert store.state.aux_originals == {"mode_select": "auto"}
