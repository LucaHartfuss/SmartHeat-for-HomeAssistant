import pytest

from heizungsbruecke.clamping import clamp


def test_clamp_within_range_returns_unchanged():
    assert clamp(0.7, 0.4, 1.5) == 0.7


def test_clamp_below_minimum_returns_minimum():
    assert clamp(0.1, 0.4, 1.5) == 0.4


def test_clamp_above_maximum_returns_maximum():
    assert clamp(2.0, 0.4, 1.5) == 1.5


def test_clamp_rejects_nan():
    # Whole-branch review finding I2: max(min(nan, maximum), minimum) degenerates to
    # nan (NaN comparisons are always False), so the old min/max idiom let NaN sail
    # through the safety clamp unchanged instead of being rejected. json.loads() accepts
    # the bare "NaN" token, and float() happily parses the string "nan", so a NaN can
    # reach here from an HA entity state or an MQTT down-message payload.
    with pytest.raises(ValueError):
        clamp(float("nan"), 0.4, 1.5)


def test_clamp_still_clamps_positive_infinity_to_maximum():
    # Pre-fix behaviour that must survive the NaN guard: +inf is a valid (if extreme)
    # finite-comparable float and correctly clamps to the upper bound.
    assert clamp(float("inf"), 0.4, 1.5) == 1.5


def test_clamp_still_clamps_negative_infinity_to_minimum():
    assert clamp(float("-inf"), 0.4, 1.5) == 0.4
