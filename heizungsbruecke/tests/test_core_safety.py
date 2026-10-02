import dataclasses

import pytest

from smartheat_core.safety import LOCAL_SAFETY, LocalSafety, check_invariants, resolve_local_safety, verteilsysteme


def test_vaillant_heizkoerper_values():
    """Regel 4, vom Nutzer freigegeben (2026-09-29/30, Heizgrenze 23 seit 2026-10-01) -- unveraendert uebernommen."""
    safety = resolve_local_safety("vaillant_vrc720", "Heizkoerper")
    assert safety == LocalSafety(
        ranges={"curve": (0.4, 1.5), "room_setpoint": (15.0, 25.0), "heat_limit": (5.0, 23.0), "min_flow": (20.0, 30.0)},
        comfort_boost={"curve": 1.5, "room_setpoint": 25.0, "heat_limit": 23.0},
        emergency_boost_levers=("curve", "room_setpoint", "heat_limit"),
        arrival_threshold_k=0.5,
    )
    assert verteilsysteme() == ("Heizkoerper",)


@pytest.mark.parametrize(("lever_set", "verteilsystem"), [
    ("vaillant_vrc720", "Fussbodenheizung"), ("vaillant_vrc720", None), ("unbekannt", "Heizkoerper"),
    ("vaillant_vrc720", ""), ("vaillant_vrc720", "heizkoerper"), ("vaillant_vrc720", "Unbekannt"),
])
def test_missing_values_are_an_error(lever_set, verteilsystem):
    with pytest.raises(ValueError, match="keine lokalen Sicherheitswerte"):
        resolve_local_safety(lever_set, verteilsystem)


def test_missing_verteilsystem_keeps_the_customer_text():
    with pytest.raises(ValueError) as error:
        resolve_local_safety("vaillant_vrc720", "Fussbodenheizung")
    assert str(error.value) == "keine lokalen Sicherheitswerte für Verteilsystem 'Fussbodenheizung'"


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


def test_heizkoerper_heat_limit_range_is_five_to_twenty_three():
    # TP13 (Regel 4, Nutzer-Entscheidung 2026-10-01): Obergrenze 20 -> 23 fuer die verankerte Heizkurve.
    assert resolve_local_safety("vaillant_vrc720", "Heizkoerper").ranges["heat_limit"] == (5.0, 23.0)
