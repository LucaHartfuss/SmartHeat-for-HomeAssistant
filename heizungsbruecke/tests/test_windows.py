import pytest

from heizungsbruecke.windows import (
    WINDOW_OPTION_KEYS,
    WindowDefaults,
    check_window_invariants,
    parse_hhmm_minutes,
    window_size_hours,
    windows_from_options,
)

VALID = {
    "daily_trigger_time": "12:00",
    "day_avg_window_start": "14:00", "day_avg_window_end": "17:00",
    "night_avg_window_start": "04:00", "night_avg_window_end": "07:00",
}


def test_window_option_keys():
    assert WINDOW_OPTION_KEYS == tuple(VALID)


def test_windows_from_options_valid():
    assert windows_from_options({**VALID, "tenant_id": "x"}) == WindowDefaults(**VALID)


def test_window_size_hours():
    assert window_size_hours("14:00", "17:00") == 3.0
    assert window_size_hours("04:00", "07:30") == 3.5


@pytest.mark.parametrize("key", list(VALID))
def test_windows_from_options_names_missing_option(key):
    options = {k: v for k, v in VALID.items() if k != key}

    with pytest.raises(ValueError, match=key):
        windows_from_options(options)


@pytest.mark.parametrize("key", list(VALID))
def test_windows_from_options_treats_empty_as_missing(key):
    with pytest.raises(ValueError, match=key):
        windows_from_options({**VALID, key: ""})


@pytest.mark.parametrize("value", ["4:00", " 04:00", "24:00", "12.00", "12:60", "1200", 1200])
def test_windows_from_options_rejects_bad_time(value):
    with pytest.raises(ValueError, match="day_avg_window_start"):
        windows_from_options({**VALID, "day_avg_window_start": value})


@pytest.mark.parametrize("overrides, fragment", [
    ({"day_avg_window_end": "18:00"}, "gleich gross"),
    ({"day_avg_window_start": "17:00", "day_avg_window_end": "14:00"}, "Tagesfenster"),
    ({"night_avg_window_start": "23:00", "night_avg_window_end": "02:00"}, "Nachtfenster"),
])
def test_windows_from_options_checks_invariants(overrides, fragment):
    with pytest.raises(ValueError, match=fragment):
        windows_from_options({**VALID, **overrides})


def test_check_window_invariants_accepts_valid():
    check_window_invariants(WindowDefaults(**VALID))


def test_parse_hhmm_minutes():
    assert parse_hhmm_minutes("00:00") == 0
    assert parse_hhmm_minutes("23:59") == 23 * 60 + 59
