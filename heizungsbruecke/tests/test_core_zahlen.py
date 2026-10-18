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


#: Grenze des float-Bereichs (IEEE 754, doppelte Genauigkeit): (2**53-1)*2**971 ist der groesste endliche float;
#: 2**1024-2**970 liegt genau in der Mitte zum naechsten Schritt und rundet gerade auf Unendlich, eins darunter noch auf
#: den groessten float. Werte als Literal, nicht aus sys.float_info.
@pytest.mark.parametrize(("value", "endlich"), [
    pytest.param((2**53 - 1) * 2**971, True, id="groesster-float-positiv"),
    pytest.param(-(2**53 - 1) * 2**971, True, id="groesster-float-negativ"),
    pytest.param(2**1024 - 2**970 - 1, True, id="eins-unter-der-grenze-positiv"),
    pytest.param(-(2**1024 - 2**970 - 1), True, id="eins-unter-der-grenze-negativ"),
    pytest.param(2**1024 - 2**970, False, id="grenze-positiv"),
    pytest.param(-(2**1024 - 2**970), False, id="grenze-negativ"),
    pytest.param(2**1024, False, id="2hoch1024-positiv"),
    pytest.param(-(2**1024), False, id="2hoch1024-negativ"),
])
def test_ganzzahl_an_der_grenze_des_float_bereichs_ohne_overflow_error(value, endlich):
    assert ist_endliche_zahl(value) is endlich
