import pytest

from heizungsbruecke.safety import LOCAL_SAFETY_BY_VERTEILSYSTEM, LocalSafety, resolve_local_safety


def test_heizkoerper_values_are_unchanged():
    # Regel 4: exakt die bisherigen Vaillant-Werte (LOCAL_CLAMP_DEFAULTS/LOCAL_BOOST_DEFAULTS bis 0.17.0).
    assert resolve_local_safety("Heizkoerper") == LocalSafety(
        curve_min=0.4, curve_max=1.5, offset_min=20.0, offset_max=30.0,
        boost_threshold_k=0.5, boost_curve_value=1.5, boost_offset_value=30.0,
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
        assert safety.offset_min <= safety.offset_max, name
        assert safety.curve_min <= safety.boost_curve_value <= safety.curve_max, name
        assert safety.offset_min <= safety.boost_offset_value <= safety.offset_max, name


def test_resolve_local_safety_rejects_inverted_clamps(monkeypatch):
    monkeypatch.setitem(LOCAL_SAFETY_BY_VERTEILSYSTEM, "Heizkoerper", LocalSafety(
        curve_min=2.0, curve_max=1.5, offset_min=20.0, offset_max=30.0,
        boost_threshold_k=0.5, boost_curve_value=1.5, boost_offset_value=30.0,
    ))

    with pytest.raises(ValueError, match="curve_min"):
        resolve_local_safety("Heizkoerper")
