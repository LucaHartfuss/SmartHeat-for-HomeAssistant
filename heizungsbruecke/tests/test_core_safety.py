import dataclasses

import pytest

from smartheat_core.safety import LOCAL_SAFETY, check_invariants, resolve_local_safety, verteilsysteme


def test_verteilsysteme_cover_both_distribution_systems():
    """Plan 3b: Spiegel von VERTEILSYSTEME_MIT_LOKALEN_SICHERHEITSWERTEN im Server (Contract-Check 1)."""
    assert verteilsysteme() == ("Fussbodenheizung", "Heizkoerper")


@pytest.mark.parametrize(("lever_set", "verteilsystem"), [
    ("vaillant_vrc720", "Deckenheizung"), ("vaillant_vrc720", None), ("unbekannt", "Heizkoerper"),
    ("vaillant_vrc720", ""), ("vaillant_vrc720", "heizkoerper"), ("vaillant_vrc720", "Unbekannt"),
])
def test_missing_values_are_an_error(lever_set, verteilsystem):
    with pytest.raises(ValueError, match="keine lokalen Sicherheitswerte"):
        resolve_local_safety(lever_set, verteilsystem)


def test_missing_verteilsystem_keeps_the_customer_text():
    with pytest.raises(ValueError) as error:
        resolve_local_safety("vaillant_vrc720", "Deckenheizung")
    assert str(error.value) == "keine lokalen Sicherheitswerte für Verteilsystem 'Deckenheizung'"


@pytest.mark.parametrize(("change", "match"), [
    ({"ranges": {"curve": (0.4, 1.5)}}, "Bereiche fuer"),
    ({"ranges": {**LOCAL_SAFETY[("vaillant_vrc720", "Heizkoerper")].ranges, "curve": (1.6, 1.5)}}, "Minimum"),
    ({"ranges": {**LOCAL_SAFETY[("vaillant_vrc720", "Heizkoerper")].ranges, "min_flow": (35.0, 30.0)}}, "min_flow Minimum"),
    ({"ranges": {**LOCAL_SAFETY[("vaillant_vrc720", "Heizkoerper")].ranges, "heat_limit": (24.0, 23.0)}}, "heat_limit Minimum"),
    ({"ranges": {**LOCAL_SAFETY[("vaillant_vrc720", "Heizkoerper")].ranges, "room_setpoint": (26.0, 25.0)}}, "room_setpoint Minimum"),
    ({"comfort_boost": {"curve": 1.6}}, "Comfort-Boost curve=1.6"),
    ({"comfort_boost": {"min_flow": 25.0}}, "Comfort-Boost min_flow"),
    ({"emergency_boost_levers": ("level",)}, "Notfall-Boost"),
    ({"arrival_threshold_k": 0.0}, "Ankunftsschwelle"),
])
def test_invariants(change, match):
    broken = dataclasses.replace(LOCAL_SAFETY[("vaillant_vrc720", "Heizkoerper")], **change)
    with pytest.raises(ValueError, match=match):
        check_invariants(broken, "vaillant_vrc720", "Heizkoerper")


def test_comfort_boost_may_be_off():
    off = dataclasses.replace(LOCAL_SAFETY[("vaillant_vrc720", "Heizkoerper")], comfort_boost={})
    check_invariants(off, "vaillant_vrc720", "Heizkoerper")


def test_every_entry_is_consistent_and_boost_inside_the_ranges():
    for (lever_set_id, verteilsystem), safety in LOCAL_SAFETY.items():
        check_invariants(safety, lever_set_id, verteilsystem)
        for lever, (low, high) in safety.ranges.items():
            assert low <= high, (verteilsystem, lever)
        for lever, value in safety.comfort_boost.items():
            assert safety.ranges[lever][0] <= value <= safety.ranges[lever][1], (verteilsystem, lever)


def test_every_combination_of_lever_set_and_distribution_system_has_values():
    from smartheat_core.levers import LEVER_SETS

    assert set(LOCAL_SAFETY) == {(lever_set, system) for lever_set in LEVER_SETS for system in verteilsysteme()}


@pytest.mark.parametrize("key", sorted(
    k for k in LOCAL_SAFETY if k[1] == "Heizkoerper" and k != ("vaillant_vrc720", "Heizkoerper")))
def test_radiator_comfort_boost_is_the_local_maximum_of_every_sent_lever(key):
    from smartheat_core.levers import LEVER_SETS

    safety = resolve_local_safety(*key)
    sent = LEVER_SETS[key[0]].levers
    assert dict(safety.comfort_boost) == {lever: safety.ranges[lever][1] for lever in sent}
    assert safety.emergency_boost_levers == sent


@pytest.mark.parametrize("lever_set", ["vaillant_vrc720", "weishaupt_wwp", "weishaupt_wwp_basis", "viessmann_vicare"])
def test_floor_heating_has_no_comfort_boost_and_an_emergency_boost_on_every_sent_lever(lever_set):
    from smartheat_core.levers import LEVER_SETS

    safety = resolve_local_safety(lever_set, "Fussbodenheizung")
    assert dict(safety.comfort_boost) == {}
    assert safety.emergency_boost_levers == LEVER_SETS[lever_set].levers


def test_a_range_with_equal_minimum_and_maximum_is_valid():
    """Mutationsprobe (Mutant 69): Minimum == Maximum ist kein Fehler, nur Minimum > Maximum."""
    safety = LOCAL_SAFETY[("vaillant_vrc720", "Heizkoerper")]
    fixed = dataclasses.replace(safety, ranges={**safety.ranges, "min_flow": (25.0, 25.0)}, comfort_boost={})
    check_invariants(fixed, "vaillant_vrc720", "Heizkoerper")


def test_a_comfort_boost_value_on_the_lower_range_limit_is_valid():
    """Mutationsprobe (Mutant 74): der Comfort-Boost-Wert darf auf der unteren Bereichsgrenze liegen."""
    safety = LOCAL_SAFETY[("vaillant_vrc720", "Heizkoerper")]
    low = safety.ranges["curve"][0]
    check_invariants(dataclasses.replace(safety, comfort_boost={"curve": low}), "vaillant_vrc720", "Heizkoerper")
