import math

import pytest

from smartheat_runtime.plausibility import ROOM_ACTUAL_RANGE, ROOM_TEMP_RANGE, is_plausible


@pytest.mark.parametrize("value,expected", [
    (5.0, True), (35.0, True), (21, True), (4.99, False), (35.01, False),
    (None, False), (True, False), (math.nan, False), (math.inf, False), ("21", False),
    pytest.param(10**400, False, id="gross-positiv"), pytest.param(-10**400, False, id="gross-negativ"),  # JSON-Ganzzahlen ausserhalb des float-Bereichs
])
def test_is_plausible_room(value, expected):
    assert is_plausible(value, ROOM_TEMP_RANGE) is expected


@pytest.mark.parametrize("value,expected", [
    (-10.0, True), (-10.1, False), (35.0, True), (35.1, False), (4.9, True), (0.0, True), (-0.1, True),
    (None, False), (True, False), (math.nan, False), (math.inf, False), (-math.inf, False), ("5", False),
])
def test_is_plausible_room_actual(value, expected):
    assert is_plausible(value, ROOM_ACTUAL_RANGE) is expected


def test_room_actual_range_is_separate_from_the_setpoint_range():
    assert ROOM_ACTUAL_RANGE == (-10.0, 35.0)
    assert ROOM_TEMP_RANGE == (5.0, 35.0)
    assert is_plausible(-10.0, ROOM_TEMP_RANGE) is False
    assert is_plausible(4.9, ROOM_TEMP_RANGE) is False
