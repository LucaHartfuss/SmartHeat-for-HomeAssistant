import math

import pytest

from smartheat_runtime.plausibility import OUTDOOR_TEMP_RANGE, ROOM_TEMP_RANGE, is_plausible


def test_ranges_match_server_r4():
    # Gleich zu heizungsserver generic/messages.py::PLAUSIBLE_RANGES (room_target/outdoor_temp); der
    # Contract-Check prueft das repo-uebergreifend.
    assert ROOM_TEMP_RANGE == (5.0, 35.0)
    assert OUTDOOR_TEMP_RANGE == (-40.0, 45.0)


@pytest.mark.parametrize("value,expected", [
    (5.0, True), (35.0, True), (21, True), (4.99, False), (35.01, False),
    (None, False), (True, False), (math.nan, False), (math.inf, False), ("21", False),
])
def test_is_plausible_room(value, expected):
    assert is_plausible(value, ROOM_TEMP_RANGE) is expected
