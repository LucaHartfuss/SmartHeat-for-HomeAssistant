from heizungsbruecke.clamping import clamp


def test_clamp_within_range_returns_unchanged():
    assert clamp(0.7, 0.4, 1.5) == 0.7


def test_clamp_below_minimum_returns_minimum():
    assert clamp(0.1, 0.4, 1.5) == 0.4


def test_clamp_above_maximum_returns_maximum():
    assert clamp(2.0, 0.4, 1.5) == 1.5
