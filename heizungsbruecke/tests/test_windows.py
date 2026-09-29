import pytest

from heizungsbruecke.windows import parse_hhmm_minutes, validate_daily_trigger_time


def test_parse_hhmm_minutes():
    assert parse_hhmm_minutes("00:00") == 0
    assert parse_hhmm_minutes("23:59") == 23 * 60 + 59


@pytest.mark.parametrize("value", ["4:00", " 04:00", "24:00", "12.00", "12:60", "1200", 1200, "12:00\n"])
def test_parse_hhmm_minutes_rejects_bad_time(value):
    with pytest.raises(ValueError, match="HH:MM"):
        parse_hhmm_minutes(value)


def test_validate_daily_trigger_time_returns_the_value():
    assert validate_daily_trigger_time("12:00") == "12:00"


@pytest.mark.parametrize("value", [None, ""])
def test_validate_daily_trigger_time_names_missing_option(value):
    with pytest.raises(ValueError, match="Option 'daily_trigger_time' fehlt"):
        validate_daily_trigger_time(value)


def test_validate_daily_trigger_time_names_the_option_for_a_bad_time():
    with pytest.raises(ValueError, match="Option 'daily_trigger_time': '25:00' ist keine Uhrzeit"):
        validate_daily_trigger_time("25:00")

