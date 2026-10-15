import math

import pytest

from smartheat_runtime.plausibility import ROOM_TEMP_RANGE, is_plausible


@pytest.mark.parametrize("value,expected", [
    (5.0, True), (35.0, True), (21, True), (4.99, False), (35.01, False),
    (None, False), (True, False), (math.nan, False), (math.inf, False), ("21", False),
])
def test_is_plausible_room(value, expected):
    assert is_plausible(value, ROOM_TEMP_RANGE) is expected
