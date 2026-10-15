"""Energie-Normalisierung (Plan 3b, Spec 6.5): Tageszaehler -> monoton wachsende Summe; Summenzaehler unveraendert."""
import pytest

from smartheat_core.energy import RESET_FRACTION, normalize, umrechnen


def _run(values, kind="daily", state=None):
    state = state or {}
    sent = []
    for value in values:
        out, state = normalize(kind, state, {"thermal_heating": value}, {"thermal_heating": "kWh"})
        sent.append(out.get("thermal_heating"))
    return sent, state


def test_total_counters_pass_through_without_state():
    assert normalize("total", {}, {"primary_heating": 12.5}, {"primary_heating": "kWh"}) == ({"primary_heating": 12.5}, {})


def test_first_daily_value_is_the_base():
    sent, state = _run([3.0])
    assert sent == [3.0] and state == {"thermal_heating": {"raw": 3.0, "sum": 3.0, "einheit": "kWh"}}


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
    out, after = normalize("daily", state, {"electrical_heating": 1.0}, {"electrical_heating": "kWh"})
    assert out == {"electrical_heating": 1.0}
    assert after["thermal_heating"] == {"raw": 3.0, "sum": 3.0, "einheit": "kWh"}


def test_non_finite_values_are_left_out():
    out, state = normalize("daily", {}, {"thermal_heating": float("nan")}, {"thermal_heating": "kWh"})
    assert (out, state) == ({}, {})


def test_unknown_kind_is_an_error():
    with pytest.raises(ValueError):
        normalize("monthly", {}, {}, {})


def test_umrechnen():
    assert umrechnen(1500.0, "Wh") == (1.5, "kWh")
    assert umrechnen(2.0, "kWh") == (2.0, "kWh") and umrechnen(3.0, "m³") == (3.0, "m³")
    with pytest.raises(KeyError):
        umrechnen(1.0, "MJ")


def test_einheitenwechsel_beginnt_die_summe_neu():
    """Spec 5: der Zustand aus 1.0.0 (Wh, ohne Einheit) laeuft nicht in kWh weiter."""
    alt = {"primary_heating": {"raw": 5000.0, "sum": 1_234_567.0}}
    out, state = normalize("daily", alt, {"primary_heating": 5.0}, {"primary_heating": "kWh"})
    assert out == {"primary_heating": 5.0}
    assert state["primary_heating"] == {"raw": 5.0, "sum": 5.0, "einheit": "kWh"}
    out, state = normalize("daily", state, {"primary_heating": 7.0}, {"primary_heating": "m³"})
    assert out == {"primary_heating": 7.0} and state["primary_heating"]["einheit"] == "m³"
