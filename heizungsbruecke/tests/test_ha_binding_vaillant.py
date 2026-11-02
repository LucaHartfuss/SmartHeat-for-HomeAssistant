"""HA-Binding Vaillant: die Betriebsart der Zone als Hilfs-Ursprungswert (mode_select) und ihre Rueckstellung auf das
Zeitprogramm (auto). Hebel und Reihenfolge mit der Pipeline: test_core_pipeline_vaillant_rueckstellung.py."""
import dataclasses
import logging

import pytest
from bindung_beispiele import VAILLANT_MYPYLLANT

from heizungsbruecke.ha_binding import (
    KEINE_BETRIEBSART,
    MANUAL_HVAC_MODE,
    ZEITPROGRAMM_HVAC_MODE,
    HaPlantBinding,
)
from smartheat_runtime.roles import ChannelManifest

ZONE = "climate.zone"
MANIFEST = ChannelManifest(refs={
    "curve_current": "number.curve", "shift_current": "climate.zone::temperature", "heat_limit": "number.g",
    "min_flow": "number.mf",
})


class FakeHa:
    def __init__(self, mode="auto"):
        self.modes = {ZONE: mode}
        self.calls = []

    def get_raw_state(self, entity_id):
        mode = self.modes[entity_id]
        if isinstance(mode, Exception):
            raise mode
        if mode in ("unavailable", "unknown"):
            raise ValueError(f"{entity_id} nicht verfuegbar")
        return mode

    def set_hvac_mode(self, entity_id, mode):
        self.calls.append(("mode", entity_id, mode))
        self.modes[entity_id] = mode


def _binding(mode="auto", manifest=MANIFEST):
    ha = FakeHa(mode)
    return HaPlantBinding(ha, manifest, VAILLANT_MYPYLLANT), ha


def test_the_vaillant_description_names_mode_select_as_the_only_aux_value():
    assert VAILLANT_MYPYLLANT.aux_originals == ("mode_select",)


@pytest.mark.parametrize("mode", ["auto", "heat_cool", "heat", "cool", "off", "dry"])
def test_read_aux_gives_the_raw_mode_of_the_zone(mode):
    binding, ha = _binding(mode)
    assert binding.read_aux() == {"mode_select": mode}
    assert ha.calls == [] and binding.physical_writes == 0


def test_read_aux_raises_when_the_zone_is_not_available():
    binding, _ = _binding("unavailable")
    with pytest.raises(ValueError):
        binding.read_aux()


def test_read_aux_without_a_climate_zone_names_no_mode():
    manifest = ChannelManifest(refs={"curve_current": "number.curve", "shift_current": "number.shift"})
    binding, _ = _binding(manifest=manifest)
    assert binding.read_aux() == {"mode_select": KEINE_BETRIEBSART}


def test_a_description_without_aux_values_reads_and_allows_nothing():
    ha = FakeHa(MANUAL_HVAC_MODE)
    binding = HaPlantBinding(ha, MANIFEST, dataclasses.replace(VAILLANT_MYPYLLANT, aux_originals=()))
    assert binding.read_aux() == {}
    assert binding.aux_zulaessig("mode_select", "auto") is False
    assert ha.calls == []


@pytest.mark.parametrize(("wert", "zulaessig"), [
    ("auto", True), ("heat_cool", True), ("heat", True), ("cool", True), ("off", True),
    ("eco", False), ("dry", False), ("AUTO", False), ("auto ", False), ("", False), (KEINE_BETRIEBSART, False),
])
def test_aux_zulaessig_only_for_the_modes_of_the_zone(wert, zulaessig):
    assert _binding()[0].aux_zulaessig("mode_select", wert) is zulaessig


@pytest.mark.parametrize("name", ["setpoint_comfort", "setpoint_setback", "mode", ""])
def test_aux_zulaessig_knows_no_other_name(name):
    assert _binding()[0].aux_zulaessig(name, "auto") is False


def test_restore_aux_puts_a_manual_zone_back_on_the_schedule_if_it_was_on_it_before():
    binding, ha = _binding(MANUAL_HVAC_MODE)
    binding.restore_aux({"mode_select": "auto"})
    assert ha.calls == [("mode", ZONE, ZEITPROGRAMM_HVAC_MODE)]
    assert binding.physical_writes == 1


def test_restore_aux_leaves_a_zone_alone_that_is_on_the_schedule_already():
    binding, ha = _binding("auto")
    binding.restore_aux({"mode_select": "auto"})
    assert ha.calls == [] and binding.physical_writes == 0


@pytest.mark.parametrize("zone", ["off", "heat", "cool"])
def test_restore_aux_also_takes_a_zone_back_that_the_customer_switched_elsewhere_meanwhile(zone):
    binding, ha = _binding(zone)
    binding.restore_aux({"mode_select": "auto"})
    assert ha.calls == [("mode", ZONE, "auto")]


@pytest.mark.parametrize("original", ["heat_cool", "off", "heat", "cool"])
@pytest.mark.parametrize("zone", ["heat_cool", "auto", "off"])
def test_restore_aux_writes_nothing_if_the_customer_was_not_on_the_schedule_before(original, zone):
    binding, ha = _binding(zone)
    binding.restore_aux({"mode_select": original})
    assert ha.calls == [] and binding.physical_writes == 0


def test_restore_aux_keeps_an_unknown_original_and_says_so(caplog):
    caplog.set_level(logging.INFO)
    binding, ha = _binding(MANUAL_HVAC_MODE)
    binding.restore_aux({"mode_select": "dry"})
    assert ha.calls == [] and binding.physical_writes == 0
    meldungen = [record.getMessage() for record in caplog.records]
    assert len(meldungen) == 1 and "dry" in meldungen[0]


def test_restore_aux_without_a_zone_mode_does_nothing_and_logs_nothing(caplog):
    caplog.set_level(logging.INFO)
    binding, ha = _binding(MANUAL_HVAC_MODE)
    binding.restore_aux({"mode_select": KEINE_BETRIEBSART})
    binding.restore_aux({})
    assert ha.calls == [] and caplog.records == []


def test_restore_aux_logs_the_switch_without_the_entity_name(caplog):
    caplog.set_level(logging.INFO)
    binding, _ = _binding(MANUAL_HVAC_MODE)
    binding.restore_aux({"mode_select": "auto"})
    meldungen = [record.getMessage() for record in caplog.records]
    assert len(meldungen) == 1 and "climate" not in meldungen[0] and "zone" not in meldungen[0].replace("Zone", "")


def test_a_failing_switch_raises_and_counts_nothing():
    class Broken(FakeHa):
        def set_hvac_mode(self, entity_id, mode):
            raise RuntimeError("myVAILLANT-Cloud nicht erreichbar")

    binding = HaPlantBinding(Broken(MANUAL_HVAC_MODE), MANIFEST, VAILLANT_MYPYLLANT)
    with pytest.raises(RuntimeError):
        binding.restore_aux({"mode_select": "auto"})
    assert binding.physical_writes == 0
