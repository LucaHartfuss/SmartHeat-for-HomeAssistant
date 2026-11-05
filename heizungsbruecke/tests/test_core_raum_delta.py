"""Die Abweichung des Ist vom Ziel ueber die Referenzraeume (reine Rechnung), Zwilling der Rechnung des Servers."""
import pytest
from hypothesis import given
from hypothesis import strategies as st

from smartheat_core import raum_delta
from smartheat_device import wire as device_protocol


def test_ein_referenzraum_ohne_versatz_ist_das_bisherige_soll_minus_ist():
    for soll, ist in [(21.0, 20.0), (21.0, 22.5), (15.0, 15.0), (25.0, 24.4), (18.5, 21.0)]:
        assert raum_delta.delta(soll, {1: ist}, {}) == pytest.approx(soll - ist)
        assert raum_delta.delta(soll, {1: ist}, {1: 0.0}) == pytest.approx(soll - ist)


def test_ausserhalb_der_raumgrenzen_gilt_ohne_versatz_das_soll_unveraendert():
    """Ein Soll unter 15 oder ueber 25 (Frostschutz, Abwesenheit) wird nicht in den Bereich gezogen: sonst loeste die
    Begrenzung bei einem Referenzraum ohne Versatz einen Boost aus, den die bisherige Rechnung nie ausloeste."""
    assert raum_delta.delta(5.0, {1: 14.0}, {1: 0.0}) == pytest.approx(-9.0)
    assert raum_delta.delta(30.0, {1: 20.0}, {}) == pytest.approx(10.0)


def test_mittel_ueber_zwei_und_drei_referenzraeume():
    assert raum_delta.delta(21.0, {1: 20.0, 2: 22.0}, {1: 0.0, 2: 1.0}) == pytest.approx(0.5)
    assert raum_delta.delta(20.0, {1: 19.0, 2: 18.5, 3: 21.0}, {2: -1.0, 3: 1.0}) == pytest.approx(0.5)


def test_ein_raum_ohne_gueltigen_ist_zaehlt_nicht_mit():
    assert raum_delta.delta(21.0, {1: 20.0}, {1: 0.0, 2: 2.0}) == pytest.approx(1.0)


def test_ohne_gueltigen_referenzraum_gibt_es_kein_delta():
    assert raum_delta.delta(21.0, {}, {1: 0.0}) is None


@pytest.mark.parametrize("versatz, erwartet", [(3.0, 2.0), (-3.0, -4.0), (0.0, -1.0)])
def test_versatz_randwerte_wirken_im_ziel(versatz, erwartet):
    assert raum_delta.delta(20.0, {1: 21.0}, {1: versatz}) == pytest.approx(erwartet)


@pytest.mark.parametrize("soll, versatz, ist, erwartet", [
    (24.5, 3.0, 24.0, 1.0),      # 27,5 wird auf 25 begrenzt
    (15.5, -3.0, 16.0, -1.0),    # 12,5 wird auf 15 begrenzt
    (24.0, 1.0, 24.0, 1.0),      # genau 25 bleibt
    (16.0, -1.0, 15.0, 0.0),     # genau 15 bleibt
    (12.0, 3.0, 14.0, -2.0),     # Soll unter 15: das Ziel ist das Soll, der Versatz hebt nicht (auch nicht bis 15)
    (13.0, 3.0, 14.0, -1.0),     # auch nicht ueber 15
    (14.9, 3.0, 14.0, 0.9),      # Randwert knapp unter 15
    (15.0, 3.0, 14.0, 4.0),      # Randwert 15: der Versatz wirkt wieder (Ziel 18)
    (12.0, -3.0, 14.0, -2.0),    # und senkt nie unter das Soll
])
def test_ziel_wird_auf_15_bis_25_begrenzt(soll, versatz, ist, erwartet):
    assert raum_delta.delta(soll, {1: ist}, {1: versatz}) == pytest.approx(erwartet)


@pytest.mark.parametrize("soll", [5.0, 12.0, 14.9])
@pytest.mark.parametrize("versatz", [-3.0, -1.0, 0.0, 1.0, 3.0])
def test_bei_soll_unter_15_ist_das_ziel_das_soll_fuer_jeden_versatz(soll, versatz):
    """Frostschutz und Abwesenheit: das Ziel steigt nie ueber das Soll, ein positiver Versatz heizt nicht an."""
    assert raum_delta.ziel(soll, versatz) == soll


def test_ab_soll_15_wirkt_der_versatz_im_bereich():
    assert raum_delta.ziel(15.0, 3.0) == 18.0
    assert raum_delta.ziel(15.0, -3.0) == 15.0
    assert raum_delta.ziel(25.0, 3.0) == 25.0

@given(soll=st.floats(min_value=0.0, max_value=14.99, allow_nan=False), versatz=st.floats(-3.0, 3.0))
def test_das_ziel_steigt_bei_soll_unter_15_nie_ueber_das_soll(soll, versatz):
    assert raum_delta.ziel(soll, versatz) == soll

def test_die_grenzen_gleichen_denen_des_geraeteprotokolls():
    assert (raum_delta.ZIEL_MIN, raum_delta.ZIEL_MAX) == (device_protocol.RAUM_SOLL_MIN, device_protocol.RAUM_SOLL_MAX)


_temperatur = st.floats(min_value=5.0, max_value=35.0, allow_nan=False)


@given(soll=_temperatur, ist=st.dictionaries(st.integers(1, 8), _temperatur, min_size=1, max_size=4),
       versatz=st.dictionaries(st.integers(1, 8), st.floats(-3.0, 3.0), max_size=8))
def test_das_ziel_verlaesst_nie_den_bereich_zwischen_soll_und_15_bis_25(soll, ist, versatz):
    ergebnis = raum_delta.delta(soll, ist, versatz)
    assert ergebnis is not None
    for raum in ist:
        ziel = raum_delta.ziel(soll, versatz.get(raum, 0.0))
        assert min(soll, raum_delta.ZIEL_MIN) <= ziel <= max(soll, raum_delta.ZIEL_MAX)
        if versatz.get(raum, 0.0) == 0.0:
            assert ziel == soll
        if soll < raum_delta.ZIEL_MIN:
            assert ziel == soll
        elif soll <= raum_delta.ZIEL_MAX:
            assert raum_delta.ZIEL_MIN <= ziel <= raum_delta.ZIEL_MAX


@pytest.mark.parametrize("ist", [20.1, 0.1 + 0.2 + 20.0, 19.999999999999996, 24.4])
def test_wirksames_ist_ohne_versatz_ist_das_ist_selbst_ohne_rundungsrauschen(ist):
    """`Soll - Delta` waere bei einem Referenzraum ohne Versatz nur auf ein ULP genau das Ist: die Regelung muss exakt
    dasselbe Ist sehen wie bisher."""
    for soll in (21.0, 20.3, 5.0, 30.0):
        assert raum_delta.wirksames_ist(soll, {1: ist}, {}) == ist


def test_wirksames_ist_ist_soll_minus_delta():
    for soll, ist, versatz in [(21.0, {1: 20.0, 2: 22.0}, {2: 1.0}), (20.0, {1: 19.0, 2: 18.5, 3: 21.0}, {2: -1.0, 3: 1.0}),
                               (24.5, {1: 24.0}, {1: 3.0}), (12.0, {1: 14.0}, {1: 3.0})]:
        assert raum_delta.wirksames_ist(soll, ist, versatz) == pytest.approx(soll - raum_delta.delta(soll, ist, versatz))


def test_wirksames_ist_ohne_referenzraum_ist_none():
    assert raum_delta.wirksames_ist(21.0, {}, {1: 0.0}) is None
