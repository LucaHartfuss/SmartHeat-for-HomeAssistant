import dataclasses

import pytest

from smartheat_core.binding import (
    BINDINGS,
    ENERGY_DAILY,
    VAILLANT_MYPYLLANT,
    VIESSMANN_VICARE_BINDING,
    WEISHAUPT_MODBUS,
    WEISHAUPT_MODBUS_BASIS,
    with_poll_interval,
)
from smartheat_core.levers import VAILLANT_VRC720, VIESSMANN_VICARE, WEISHAUPT_WWP, WEISHAUPT_WWP_BASIS


def test_vaillant_mypyllant_description():
    d = VAILLANT_MYPYLLANT
    assert d.lever_set == VAILLANT_VRC720
    assert d.steps == {"curve": 0.05, "room_setpoint": 0.5, "heat_limit": 0.1, "min_flow": 0.1}
    assert d.enforce_tolerance == {"curve": 0.025, "room_setpoint": 0.25, "heat_limit": 0.05, "min_flow": 0.1}
    assert d.settle_seconds == 2100
    assert (d.restore_originals, d.optional_restore, d.prepared_lever) == (("heat_limit",), ("heat_limit",), "room_setpoint")
    assert d.labels == {
        "curve": "Heizkurve", "room_setpoint": "Wunschtemperatur der Zone", "heat_limit": "Heizgrenze",
        "min_flow": "Mindestvorlauftemperatur",
    }
    assert d.preparation_label == "Betriebsart der Zone"
    assert BINDINGS["vaillant_vrc720"] is VAILLANT_MYPYLLANT


def test_vaillant_keeps_the_defaults_of_the_plan_3b_fields():
    """Plan 3b: die neuen Felder lassen Vaillant unveraendert (feste Wartezeit, kein Tagesbudget, 6 Durchsetzungen,
    keine Gruppen, keine Hilfswerte); Tageszaehler seit Audit 4 P-B (A4-49, myVAILLANT setzt die Energiesensoren um
    Mitternacht zurueck)."""
    d = VAILLANT_MYPYLLANT
    assert d.preparation_resets_setpoint is True
    assert (d.settle_min_seconds, d.default_poll_seconds, d.daily_write_limit, d.lifetime_hint_at) == (None,) * 4
    assert (d.enforce_per_day, d.write_groups, d.aux_originals, d.readonly_levers) == (6, (), (), ())
    assert d.energy_counters == ENERGY_DAILY
    assert with_poll_interval(d, 10) is d


def test_weishaupt_descriptions():
    for d, lever_set in ((WEISHAUPT_MODBUS, WEISHAUPT_WWP), (WEISHAUPT_MODBUS_BASIS, WEISHAUPT_WWP_BASIS)):
        assert d.lever_set == lever_set
        assert d.steps == {lever: {"curve": 0.05, "room_setpoint": 0.5, "heat_limit": 0.5}[lever] for lever in lever_set.levers}
        assert all(d.enforce_tolerance[lever] == d.steps[lever] / 2 for lever in lever_set.levers)
        assert d.restore_originals == lever_set.levers and d.optional_restore == ()
        assert (d.prepared_lever, d.preparation_label, d.preparation_resets_setpoint) == ("room_setpoint", "Betriebsart", False)
        assert (d.settle_seconds, d.settle_min_seconds, d.default_poll_seconds) == (120, 120, 30)
        assert (d.daily_write_limit, d.lifetime_hint_at, d.enforce_per_day, d.write_groups) == (10, 50000, 6, ())
        assert d.aux_originals == ("mode_select", "setpoint_comfort", "setpoint_setback")
        assert d.energy_counters == ENERGY_DAILY
    assert WEISHAUPT_MODBUS.labels == {
        "curve": "Heizkennlinie", "room_setpoint": "Raumsolltemperatur Normal", "heat_limit": "Sommer-Winter-Umschaltung",
    }
    assert (WEISHAUPT_MODBUS.readonly_levers, WEISHAUPT_MODBUS_BASIS.readonly_levers) == ((), ("curve",))


def test_viessmann_description():
    d = VIESSMANN_VICARE_BINDING
    assert d.lever_set == VIESSMANN_VICARE
    assert d.steps == {"curve": 0.1, "level": 1.0, "room_setpoint": 1.0}
    assert d.enforce_tolerance == {"curve": 0.05, "level": 0.5, "room_setpoint": 0.5}
    assert (d.settle_seconds, d.settle_min_seconds, d.default_poll_seconds) == (180, 180, 60)
    assert d.restore_originals == ("curve", "level", "room_setpoint")
    assert (d.prepared_lever, d.preparation_label, d.preparation_resets_setpoint) == ("room_setpoint", "Heizprogramm", False)
    assert (d.daily_write_limit, d.lifetime_hint_at, d.enforce_per_day) == (None, None, 4)
    assert d.write_groups == (("curve", "level"),)
    assert d.aux_originals == ("mode_select",) and d.energy_counters == ENERGY_DAILY
    assert d.labels == {
        "curve": "Neigung der Heizkurve", "level": "Niveau der Heizkurve", "room_setpoint": "Raumtemperatur Normal",
    }


def test_bindings_cover_every_lever_set_of_the_core():
    from smartheat_core.levers import LEVER_SETS

    assert set(BINDINGS) == set(LEVER_SETS)
    assert all(description.lever_set is LEVER_SETS[lever_set] for lever_set, description in BINDINGS.items())


@pytest.mark.parametrize(("description", "poll", "settle"), [
    (WEISHAUPT_MODBUS, None, 120), (WEISHAUPT_MODBUS, 30, 120), (WEISHAUPT_MODBUS, 10, 120),
    (WEISHAUPT_MODBUS, 90, 240), (VIESSMANN_VICARE_BINDING, None, 180), (VIESSMANN_VICARE_BINDING, 60, 180),
    (VIESSMANN_VICARE_BINDING, 30, 180), (VIESSMANN_VICARE_BINDING, 300, 660), (VAILLANT_MYPYLLANT, 1800, 2100),
])
def test_settle_time_follows_the_poll_interval(description, poll, settle):
    effective = with_poll_interval(description, poll)
    assert effective.settle_seconds == settle
    assert dataclasses.replace(effective, settle_seconds=description.settle_seconds) == description


@pytest.mark.parametrize(("change", "match"), [
    ({"steps": {"curve": 0.05}}, "Schrittweite"),
    ({"enforce_tolerance": {"curve": 0.025}}, "Toleranz"),
    ({"labels": {}}, "Anzeigename"),
    ({"restore_originals": ("level",)}, "restore_originals"),
    ({"optional_restore": ("min_flow",)}, "optional_restore"),
    ({"prepared_lever": "level"}, "prepared_lever"),
    ({"settle_seconds": 0}, "Wartezeit"),
    ({"settle_min_seconds": 120.0}, "nur zusammen"),
    ({"daily_write_limit": 0}, "daily_write_limit"),
    ({"lifetime_hint_at": 0}, "lifetime_hint_at"),
    ({"enforce_per_day": 0}, "enforce_per_day"),
    ({"write_groups": (("curve",),)}, "Schreibgruppe"),
    ({"write_groups": (("curve", "level"),)}, "Schreibgruppe"),
    ({"write_groups": (("curve", "room_setpoint"), ("room_setpoint", "heat_limit"))}, "Schreibgruppe"),
    ({"energy_counters": "monthly"}, "energy_counters"),
    ({"readonly_levers": ("curve",)}, "readonly_levers"),
    ({"readonly_levers": ("slope",)}, "readonly_levers"),
])
def test_description_is_validated(change, match):
    with pytest.raises(ValueError, match=match):
        dataclasses.replace(VAILLANT_MYPYLLANT, **change)
