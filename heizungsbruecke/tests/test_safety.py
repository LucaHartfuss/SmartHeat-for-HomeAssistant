from dataclasses import replace

import pytest

from heizungsbruecke.safety import LOCAL_SAFETY_BY_VERTEILSYSTEM, LocalSafety, resolve_local_safety


def test_heizkoerper_values_approved_2026_09_29():
    # Regel 4, vom Nutzer am 2026-09-29 freigegeben (TP11): Parallelverschiebung/Mindestvorlauf
    # loesen den bisherigen "Offset" ab.
    # Heat limit max erhoehung 20 -> 23 in TP13 (2026-10-01, Regel 4).
    assert LOCAL_SAFETY_BY_VERTEILSYSTEM["Heizkoerper"] == LocalSafety(
        curve_min=0.4, curve_max=1.5, shift_min=15.0, shift_max=25.0, min_flow_min=20.0, min_flow_max=30.0,
        boost_threshold_k=0.5, boost_curve_value=1.5, boost_shift_value=25.0,
        heat_limit_min=5.0, heat_limit_max=23.0,
    )


def test_only_heizkoerper_has_values():
    assert set(LOCAL_SAFETY_BY_VERTEILSYSTEM) == {"Heizkoerper"}


@pytest.mark.parametrize("verteilsystem", ["Fussbodenheizung", "", None, "heizkoerper", "Unbekannt"])
def test_resolve_local_safety_rejects(verteilsystem):
    with pytest.raises(ValueError, match="keine lokalen Sicherheitswerte"):
        resolve_local_safety(verteilsystem)


def test_every_entry_has_consistent_clamps_and_boost_inside_them():
    for name, safety in LOCAL_SAFETY_BY_VERTEILSYSTEM.items():
        assert safety.curve_min <= safety.curve_max, name
        assert safety.shift_min <= safety.shift_max, name
        assert safety.min_flow_min <= safety.min_flow_max, name
        assert safety.heat_limit_min <= safety.heat_limit_max, name
        assert safety.curve_min <= safety.boost_curve_value <= safety.curve_max, name
        assert safety.shift_min <= safety.boost_shift_value <= safety.shift_max, name


def test_resolve_local_safety_rejects_inverted_clamps(monkeypatch):
    monkeypatch.setitem(LOCAL_SAFETY_BY_VERTEILSYSTEM, "Heizkoerper", LocalSafety(
        curve_min=2.0, curve_max=1.5, shift_min=15.0, shift_max=25.0, min_flow_min=20.0, min_flow_max=30.0,
        boost_threshold_k=0.5, boost_curve_value=1.5, boost_shift_value=25.0,
        heat_limit_min=5.0, heat_limit_max=20.0,
    ))

    with pytest.raises(ValueError, match="curve_min"):
        resolve_local_safety("Heizkoerper")


def test_resolve_local_safety_rejects_inverted_min_flow_clamps(monkeypatch):
    monkeypatch.setitem(LOCAL_SAFETY_BY_VERTEILSYSTEM, "Heizkoerper", LocalSafety(
        curve_min=0.4, curve_max=1.5, shift_min=15.0, shift_max=25.0, min_flow_min=35.0, min_flow_max=30.0,
        boost_threshold_k=0.5, boost_curve_value=1.5, boost_shift_value=25.0,
        heat_limit_min=5.0, heat_limit_max=20.0,
    ))

    with pytest.raises(ValueError, match="min_flow_min"):
        resolve_local_safety("Heizkoerper")


def test_heizkoerper_heat_limit_clamps_are_five_to_twenty_three():
    # TP13 (Regel 4, Nutzer-Entscheidung 2026-10-01): Obergrenze 20 -> 23 fuer die verankerte Heizkurve.
    safety = resolve_local_safety("Heizkoerper")
    assert (safety.heat_limit_min, safety.heat_limit_max) == (5.0, 23.0)


def test_heat_limit_min_above_max_is_rejected(monkeypatch):
    broken = replace(LOCAL_SAFETY_BY_VERTEILSYSTEM["Heizkoerper"], heat_limit_min=24.0)
    monkeypatch.setitem(LOCAL_SAFETY_BY_VERTEILSYSTEM, "Heizkoerper", broken)
    with pytest.raises(ValueError, match="heat_limit_min"):
        resolve_local_safety("Heizkoerper")
