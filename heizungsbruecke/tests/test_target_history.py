import pytest

from heizungsbruecke.target_history import WINDOW_SECONDS, record_change, time_weighted_mean

NOW = 1_000_000.0
H = 3600.0


def test_empty_history_has_no_mean():
    assert time_weighted_mean([], NOW) is None


def test_constant_value_mean_is_that_value():
    assert time_weighted_mean([[NOW - 48 * H, 21.0]], NOW) == pytest.approx(21.0)


def test_mean_with_one_change_inside_window():
    history = [[NOW - 48 * H, 20.0], [NOW - 6 * H, 22.0]]
    assert time_weighted_mean(history, NOW) == pytest.approx((18 * 20.0 + 6 * 22.0) / 24)


def test_mean_uses_last_value_before_window_start():
    history = [[NOW - 72 * H, 18.0], [NOW - 30 * H, 20.0], [NOW - 12 * H, 22.0]]
    assert time_weighted_mean(history, NOW) == pytest.approx((12 * 20.0 + 12 * 22.0) / 24)


def test_short_history_averages_over_known_span_only():
    history = [[NOW - 4 * H, 20.0], [NOW - 2 * H, 22.0]]
    assert time_weighted_mean(history, NOW) == pytest.approx(21.0)


def test_record_change_ignores_unchanged_value():
    history = [[NOW - H, 21.0]]
    assert record_change(history, NOW, 21.0) == history


def test_record_change_appends_new_value():
    assert record_change([[NOW - H, 21.0]], NOW, 22.0) == [[NOW - H, 21.0], [NOW, 22.0]]


def test_record_change_prunes_but_keeps_last_entry_before_window():
    history = [[NOW - 72 * H, 18.0], [NOW - 30 * H, 20.0], [NOW - 12 * H, 22.0]]
    assert record_change(history, NOW, 23.0) == [[NOW - 30 * H, 20.0], [NOW - 12 * H, 22.0], [NOW, 23.0]]


def test_record_change_seeds_empty_history():
    assert record_change([], NOW, 21.0) == [[NOW, 21.0]]


def test_window_constant():
    assert WINDOW_SECONDS == 86400


def test_mean_ignores_part_of_future_dated_entry():
    # Clock jump backwards: entry recorded after boot has "future" timestamp
    # history: [[NOW-10H,20.0],[NOW-5H,25.0],[NOW+5H,30.0]]
    # Entry 3 hasn't happened yet from now's perspective; entry 2 runs until now
    # Correct mean: 20.0 for 5h + 25.0 for 5h = (20*5 + 25*5) / 10 = 22.5
    history = [[NOW - 10 * H, 20.0], [NOW - 5 * H, 25.0], [NOW + 5 * H, 30.0]]
    assert time_weighted_mean(history, NOW) == pytest.approx(22.5)


def test_mean_with_entry_exactly_at_window_start():
    # Entry exactly at window boundary
    history = [[NOW - 24 * H, 20.0], [NOW - 12 * H, 22.0]]
    assert time_weighted_mean(history, NOW) == pytest.approx(21.0)
