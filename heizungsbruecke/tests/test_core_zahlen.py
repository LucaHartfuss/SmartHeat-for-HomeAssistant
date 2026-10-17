import math

import pytest

from smartheat_core.zahlen import ist_endliche_zahl


@pytest.mark.parametrize("value", [0, 1, -3, 0.0, 21.5, -40.0, 10**300, -10**300, 2**1023])
def test_finite_int_and_float_are_finite_numbers(value):
    assert ist_endliche_zahl(value) is True


@pytest.mark.parametrize("value", [
    math.nan, math.inf, -math.inf, True, False, None, "21", [1], {"a": 1},
    pytest.param(10**400, id="int-gross-positiv"), pytest.param(-10**400, id="int-gross-negativ"),
])
def test_everything_else_is_not_a_finite_number_and_never_raises(value):
    assert ist_endliche_zahl(value) is False
