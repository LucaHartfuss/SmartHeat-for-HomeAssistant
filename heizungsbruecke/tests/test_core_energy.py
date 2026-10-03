"""Energie-Normalisierung (Plan 3b, Spec 6.5): Tageszaehler -> monoton wachsende Summe; Summenzaehler unveraendert."""
import pytest

from smartheat_core.energy import RESET_FRACTION, normalize


def _run(values, kind="daily", state=None):
    state = state or {}
    sent = []
    for value in values:
        out, state = normalize(kind, state, {"thermal_heating": value})
        sent.append(out.get("thermal_heating"))
    return sent, state


def test_total_counters_pass_through_without_state():
    assert normalize("total", {}, {"primary_heating": 12.5}) == ({"primary_heating": 12.5}, {})


def test_first_daily_value_is_the_base():
    sent, state = _run([3.0])
    assert sent == [3.0] and state == {"thermal_heating": {"raw": 3.0, "sum": 3.0}}


def test_daily_counter_grows_and_continues_after_midnight():
    sent, _ = _run([3.0, 5.5, 9.0, 0.4, 2.0])
    assert sent == [3.0, 5.5, 9.0, 9.4, 11.0]


def test_small_decrease_is_noise_not_a_reset():
    sent, state = _run([9.0, 8.9, 9.5])
    assert sent == [9.0, 9.0, 9.6]
    assert state["thermal_heating"]["raw"] == 9.5


def test_reset_threshold():
    assert RESET_FRACTION == 0.5
    sent, _ = _run([8.0, 3.9])  # unter der Haelfte: Ruecksetzung
    assert sent == [8.0, 11.9]


def test_restart_continues_from_the_saved_state():
    _, state = _run([3.0, 5.0])
    sent, _ = _run([0.5], state=state)  # Neustart ueber Mitternacht
    assert sent == [5.5]


def test_a_missing_channel_keeps_its_state():
    _, state = _run([3.0])
    out, after = normalize("daily", state, {"electrical_heating": 1.0})
    assert out == {"electrical_heating": 1.0}
    assert after["thermal_heating"] == {"raw": 3.0, "sum": 3.0}


def test_non_finite_values_are_left_out():
    out, state = normalize("daily", {}, {"thermal_heating": float("nan")})
    assert (out, state) == ({}, {})


def test_unknown_kind_is_an_error():
    with pytest.raises(ValueError):
        normalize("monthly", {}, {})
