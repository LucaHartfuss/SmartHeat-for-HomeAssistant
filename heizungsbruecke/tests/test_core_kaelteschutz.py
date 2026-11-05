"""Kaelteschutz-Entscheidung (reine Funktion): Soll-Ausloeser (Abweichung der Referenzraeume), Frostnaehe-Ausloeser,
Gueltigkeit der Werte, Ende bei Ankunft oder nach der Hoechstdauer, Wiederausloesung. Die Werte stehen woertlich in
`test_regel4_werte.py`; hier die Randwerte beidseitig."""
import copy
import math
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from smartheat_core import boost, emergency_boost
from smartheat_core.emergency_boost import (
    KaelteschutzZustand,
    Messwert,
    gueltige_ist,
    kaelteschutz_entscheiden,
)

T0 = datetime(2026, 11, 5, 8, 0, tzinfo=UTC)
MIN = timedelta(minutes=1)
SCHRITT = 5 * MIN  # Pruefertakt
ANKUNFT_K = 0.5


def ist(wert, jetzt, raum=1):
    return {raum: Messwert(wert, jetzt)}


def lauf(start, ende, bei, schritt=SCHRITT, zustand=None):
    """Ruft die Entscheidung von `start` bis einschliesslich `ende` im Takt `schritt` auf; `bei(jetzt)` nennt die
    Eingaben als dict(ist=..., soll=..., delta=...). Gibt (letzte Entscheidung, Liste aller Entscheidungen) zurueck."""
    zustand = zustand or KaelteschutzZustand()
    jetzt, alle = start, []
    while jetzt <= ende:
        e = kaelteschutz_entscheiden(jetzt, zustand=zustand, arrival_threshold_k=ANKUNFT_K, **bei(jetzt))
        zustand = e.zustand
        alle.append(e)
        jetzt += schritt
    return alle[-1], alle


def soll_eingang(delta, soll=21.0):
    return lambda jetzt: dict(ist=ist(soll - delta, jetzt), soll=soll, delta=delta)


def frost_eingang(wert, soll=None, delta=None, raum=1):
    return lambda jetzt: dict(ist=ist(wert, jetzt, raum), soll=soll, delta=delta)


def stunden(h, m=0):
    return T0 + timedelta(hours=h, minutes=m)


# --- Soll-Ausloeser: Abweichung >= 2,0 K durchgehend 3 h -------------------------------------------------------

def test_soll_ausloeser_nach_drei_stunden_bei_zwei_kelvin():
    letzte, alle = lauf(T0, stunden(3), soll_eingang(2.0))
    assert (letzte.art, letzte.grund, letzte.gestartet) == ("soll", "soll_abweichung", True)
    assert all(e.art == "aus" for e in alle[:-1])


def test_soll_ausloeser_nicht_nach_zwei_stunden_59():
    letzte, _ = lauf(T0, stunden(2, 59), soll_eingang(2.0), schritt=MIN)
    assert (letzte.art, letzte.grund) == ("aus", "kein_ausloeser")


def test_soll_ausloeser_knapp_unter_zwei_kelvin_loest_nie_aus():
    letzte, alle = lauf(T0, stunden(5), soll_eingang(1.9))
    assert all(e.art == "aus" for e in alle)


def test_soll_ausloeser_bei_zwei_komma_eins_kelvin():
    letzte, _ = lauf(T0, stunden(3), soll_eingang(2.1))
    assert letzte.art == "soll"


def test_soll_ausloeser_zaehlt_von_vorn_nach_einer_unterbrechung():
    zustand = lauf(T0, stunden(2, 30), soll_eingang(2.5))[1][-1].zustand
    zwischen = kaelteschutz_entscheiden(stunden(2, 35), ist=ist(20.5, stunden(2, 35)), soll=21.0, delta=0.5,
                                        zustand=zustand, arrival_threshold_k=ANKUNFT_K)
    assert zwischen.art == "aus"
    _, alle = lauf(stunden(2, 40), stunden(5, 40), soll_eingang(2.5), zustand=zwischen.zustand)
    assert [e.art for e in alle].index("soll") == 36  # erst 3 h nach 2:40, nicht nach 3:00 vom ersten Beginn


def test_ohne_soll_gibt_es_keinen_soll_ausloeser():
    letzte, alle = lauf(T0, stunden(6), lambda j: dict(ist=ist(18.0, j), soll=None, delta=5.0))
    assert all(e.art == "aus" for e in alle)


def test_ohne_delta_gibt_es_keinen_soll_ausloeser():
    letzte, alle = lauf(T0, stunden(6), lambda j: dict(ist=ist(18.0, j), soll=21.0, delta=None))
    assert all(e.art == "aus" for e in alle)


# --- Frostnaehe-Ausloeser: Raum unter 5 Grad durchgehend 15 min ------------------------------------------------

def test_frostnaehe_nach_fuenfzehn_minuten():
    letzte, alle = lauf(T0, T0 + 15 * MIN, frost_eingang(4.9))
    assert (letzte.art, letzte.grund, letzte.gestartet) == ("frost", "frostnaehe", True)
    assert all(e.art == "aus" for e in alle[:-1])


def test_frostnaehe_nicht_nach_vierzehn_minuten_59():
    zustand = KaelteschutzZustand()
    for minute in (0, 5, 10):
        zustand = kaelteschutz_entscheiden(T0 + minute * MIN, ist=ist(4.9, T0 + minute * MIN), soll=None, delta=None,
                                           zustand=zustand, arrival_threshold_k=ANKUNFT_K).zustand
    jetzt = T0 + 14 * MIN + timedelta(seconds=59)
    e = kaelteschutz_entscheiden(jetzt, ist=ist(4.9, jetzt), soll=None, delta=None, zustand=zustand,
                                 arrival_threshold_k=ANKUNFT_K)
    assert e.art == "aus"
    jetzt = T0 + 15 * MIN
    e = kaelteschutz_entscheiden(jetzt, ist=ist(4.9, jetzt), soll=None, delta=None, zustand=e.zustand,
                                 arrival_threshold_k=ANKUNFT_K)
    assert e.art == "frost"


def test_genau_fuenf_grad_loest_nicht_aus():
    _, alle = lauf(T0, stunden(2), frost_eingang(5.0))
    assert all(e.art == "aus" for e in alle)


def test_frostnaehe_jeder_raum_des_kreises_zaehlt_auch_ein_kontrollraum():
    # Raum 7 ist weder Referenzraum noch im Delta: trotzdem loest sein kaltes Ist aus.
    def bei(jetzt):
        return dict(ist={1: Messwert(20.0, jetzt), 7: Messwert(4.0, jetzt)}, soll=21.0, delta=1.0)
    letzte, _ = lauf(T0, T0 + 15 * MIN, bei)
    assert letzte.art == "frost"


def test_frostnaehe_zaehlt_je_raum_ein_wechsel_des_kalten_raums_setzt_nicht_fort():
    def bei(jetzt):
        kalt = 1 if (jetzt - T0) < 10 * MIN else 2
        return dict(ist={r: Messwert(4.0 if r == kalt else 20.0, jetzt) for r in (1, 2)}, soll=None, delta=None)
    # Raum 1 war 10 min kalt, Raum 2 ist es ab Minute 10: ausgeloest wird erst 15 min danach, nicht bei Minute 15.
    letzte, alle = lauf(T0, T0 + 25 * MIN, bei)
    assert [e.art for e in alle] == ["aus"] * 5 + ["frost"]


def test_frostnaehe_loest_auch_ohne_soll_aus():
    letzte, _ = lauf(T0, T0 + 15 * MIN, frost_eingang(3.0, soll=None, delta=None))
    assert letzte.art == "frost"


def test_frost_hat_vorrang_wenn_beide_ausloeser_zugleich_reif_sind():
    def bei(jetzt):
        return dict(ist={1: Messwert(4.0, jetzt)}, soll=21.0, delta=17.0)
    # Soll-Ausloeser nach 3 h, Frostnaehe schon nach 15 min: Frost laeuft zuerst, der Soll-Zaehler ruht danach.
    letzte, alle = lauf(T0, T0 + 15 * MIN, bei)
    assert letzte.art == "frost"


# --- Gueltigkeit ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("wert, gueltig", [
    (-10.0, True), (-10.1, False), (35.0, True), (35.1, False), (None, False),
    (math.nan, False), (math.inf, False), (-math.inf, False),
])
def test_gueltige_ist_bereich_minus_zehn_bis_fuenfunddreissig(wert, gueltig):
    assert (gueltige_ist(T0, {1: Messwert(wert, T0)}) == {1: wert}) is gueltig


@pytest.mark.parametrize("alter_min, gueltig", [(0, True), (15, True), (16, False)])
def test_luecke_ueber_fuenfzehn_minuten_macht_den_wert_ungueltig(alter_min, gueltig):
    assert (1 in gueltige_ist(T0, {1: Messwert(20.0, T0 - alter_min * MIN)})) is gueltig


def test_wert_ohne_zeitstempel_oder_aus_der_zukunft_ist_ungueltig():
    assert gueltige_ist(T0, {1: Messwert(20.0, None), 2: Messwert(20.0, T0 + MIN)}) == {}


def test_minus_zehn_komma_null_zaehlt_minus_zehn_komma_eins_nicht():
    letzte, _ = lauf(T0, T0 + 15 * MIN, frost_eingang(-10.0))
    assert letzte.art == "frost"
    letzte, alle = lauf(T0, T0 + 60 * MIN, frost_eingang(-10.1))
    assert all(e.art == "aus" for e in alle)


def test_ungueltiger_wert_dazwischen_beendet_die_zaehlung_der_frostnaehe():
    zustand = lauf(T0, T0 + 10 * MIN, frost_eingang(4.0))[1][-1].zustand
    e = kaelteschutz_entscheiden(T0 + 15 * MIN, ist={1: Messwert(None, T0 + 15 * MIN)}, soll=None, delta=None,
                                 zustand=zustand, arrival_threshold_k=ANKUNFT_K)
    assert e.art == "aus"
    e = kaelteschutz_entscheiden(T0 + 20 * MIN, ist=ist(4.0, T0 + 20 * MIN), soll=None, delta=None, zustand=e.zustand,
                                 arrival_threshold_k=ANKUNFT_K)
    assert e.art == "aus"  # waere ohne die Unterbrechung nach 15 min schon ausgeloest


def test_luecke_zwischen_zwei_pruefungen_beendet_die_zaehlung():
    zustand = lauf(T0, T0 + 10 * MIN, frost_eingang(4.0))[1][-1].zustand
    # 15 min nach der letzten Pruefung (Minute 10) ist es noch durchgehend, 16 min nicht.
    spaet = T0 + 26 * MIN
    e = kaelteschutz_entscheiden(spaet, ist=ist(4.0, spaet), soll=None, delta=None, zustand=zustand,
                                 arrival_threshold_k=ANKUNFT_K)
    assert e.art == "aus"
    gerade = T0 + 25 * MIN
    e = kaelteschutz_entscheiden(gerade, ist=ist(4.0, gerade), soll=None, delta=None, zustand=zustand,
                                 arrival_threshold_k=ANKUNFT_K)
    assert e.art == "frost"


def test_soll_zaehlung_endet_bei_luecke_ueber_fuenfzehn_minuten():
    zustand = lauf(T0, stunden(2), soll_eingang(2.5))[1][-1].zustand
    spaet = stunden(2, 16)
    e = kaelteschutz_entscheiden(spaet, ist=ist(18.5, spaet), soll=21.0, delta=2.5, zustand=zustand,
                                 arrival_threshold_k=ANKUNFT_K)
    assert e.art == "aus"
    _, alle = lauf(stunden(2, 21), stunden(5, 16), soll_eingang(2.5), zustand=e.zustand)  # Neubeginn bei 2:16
    assert [x.art for x in alle].index("soll") == len(alle) - 1


# --- laufende Entscheidung: Ankunft, Hoechstdauer, Fuehlerausfall ------------------------------------------------

def laufender_soll(delta=2.5):
    return lauf(T0, stunden(3), soll_eingang(delta))[0]


def test_soll_lauf_bleibt_bis_zur_ankunft_und_endet_dann():
    e = laufender_soll()
    jetzt = stunden(3, 5)
    e = kaelteschutz_entscheiden(jetzt, ist=ist(20.4, jetzt), soll=21.0, delta=0.6, zustand=e.zustand,
                                 arrival_threshold_k=ANKUNFT_K)
    assert (e.art, e.grund, e.gestartet) == ("soll", "laeuft", False)
    jetzt = stunden(3, 10)
    e2 = kaelteschutz_entscheiden(jetzt, ist=ist(20.5, jetzt), soll=21.0, delta=0.5, zustand=e.zustand,
                                  arrival_threshold_k=ANKUNFT_K)
    assert (e2.art, e2.grund) == ("aus", "ankunft")
    assert e2.zustand == KaelteschutzZustand()


def test_lauf_endet_nach_vier_stunden_nicht_vorher():
    e = laufender_soll()
    ende = stunden(3) + timedelta(seconds=boost.COMFORT_BOOST_MAX_SECONDS)
    jetzt = ende - SCHRITT
    # Nach 3:55 h Laufzeit: laeuft noch, weil die Abweichung bleibt (Takt von 5 min, Zustand mitfuehren).
    letzte, alle = lauf(stunden(3, 5), jetzt, soll_eingang(2.5), zustand=e.zustand)
    assert letzte.art == "soll"
    e = kaelteschutz_entscheiden(ende - timedelta(seconds=1), ist=ist(18.5, ende - SCHRITT), soll=21.0, delta=2.5,
                                 zustand=letzte.zustand, arrival_threshold_k=ANKUNFT_K)
    assert e.art == "soll"
    e = kaelteschutz_entscheiden(ende, ist=ist(18.5, ende), soll=21.0, delta=2.5, zustand=e.zustand,
                                 arrival_threshold_k=ANKUNFT_K)
    assert (e.art, e.grund) == ("aus", "hoechstdauer")


def test_hoechstdauer_ist_die_des_comfort_boosts():
    assert timedelta(seconds=boost.COMFORT_BOOST_MAX_SECONDS) == emergency_boost.KAELTESCHUTZ_HOECHSTDAUER


def test_nach_der_hoechstdauer_beginnt_ein_neuer_lauf_von_vorn():
    e = laufender_soll()
    ende = stunden(3) + timedelta(seconds=boost.COMFORT_BOOST_MAX_SECONDS)
    e = kaelteschutz_entscheiden(ende, ist=ist(18.5, ende), soll=21.0, delta=2.5, zustand=e.zustand,
                                 arrival_threshold_k=ANKUNFT_K)
    assert e.art == "aus"
    # Die Abweichung bleibt: wieder erst nach 3 h, nicht sofort.
    fruehestens = ende + timedelta(hours=3)
    letzte, alle = lauf(ende + SCHRITT, fruehestens - SCHRITT, soll_eingang(2.5), zustand=e.zustand)
    assert all(x.art == "aus" for x in alle)
    letzte, _ = lauf(ende + SCHRITT, fruehestens + SCHRITT, soll_eingang(2.5), zustand=e.zustand)
    assert (letzte.art, letzte.gestartet) == ("soll", True)


def test_nach_der_ankunft_beginnt_die_frostnaehe_wieder_mit_fuenfzehn_minuten():
    e = lauf(T0, T0 + 15 * MIN, frost_eingang(4.0))[0]
    assert e.art == "frost"
    jetzt = T0 + 20 * MIN
    e = kaelteschutz_entscheiden(jetzt, ist=ist(7.0, jetzt), soll=None, delta=None, zustand=e.zustand,
                                 arrival_threshold_k=ANKUNFT_K)
    assert (e.art, e.grund) == ("aus", "ankunft")
    letzte, alle = lauf(T0 + 25 * MIN, T0 + 35 * MIN, frost_eingang(4.0), zustand=e.zustand)
    assert all(x.art == "aus" for x in alle)
    letzte, _ = lauf(T0 + 25 * MIN, T0 + 40 * MIN, frost_eingang(4.0), zustand=e.zustand)
    assert (letzte.art, letzte.gestartet) == ("frost", True)


def test_fuehlerausfall_beendet_den_laufenden_lauf_nicht():
    e = laufender_soll()
    for minute in range(5, 200, 5):
        jetzt = stunden(3, minute)
        if jetzt - stunden(3) >= boost_dauer():
            break
        e = kaelteschutz_entscheiden(jetzt, ist={1: Messwert(None, None)}, soll=21.0, delta=None, zustand=e.zustand,
                                     arrival_threshold_k=ANKUNFT_K)
        assert (e.art, e.grund) == ("soll", "laeuft")


def boost_dauer():
    return timedelta(seconds=boost.COMFORT_BOOST_MAX_SECONDS)


# --- Ankunft ohne Soll: Hysterese 7 Grad -------------------------------------------------------------------------

def laufender_frost(soll=None, delta=None):
    return lauf(T0, T0 + 15 * MIN, frost_eingang(4.0, soll=soll, delta=delta))[0]


@pytest.mark.parametrize("wert, endet", [(6.9, False), (7.0, True), (5.0, False), (12.0, True)])
def test_ohne_soll_endet_der_lauf_erst_ab_sieben_grad(wert, endet):
    e = laufender_frost()
    jetzt = T0 + 20 * MIN
    e = kaelteschutz_entscheiden(jetzt, ist=ist(wert, jetzt), soll=None, delta=None, zustand=e.zustand,
                                 arrival_threshold_k=ANKUNFT_K)
    assert (e.art == "aus") is endet


def test_ohne_soll_muss_jeder_gueltige_raum_sieben_grad_haben():
    e = laufender_frost()
    jetzt = T0 + 20 * MIN
    e = kaelteschutz_entscheiden(jetzt, ist={1: Messwert(8.0, jetzt), 2: Messwert(6.0, jetzt)}, soll=None, delta=None,
                                 zustand=e.zustand, arrival_threshold_k=ANKUNFT_K)
    assert e.art == "frost"
    jetzt = T0 + 25 * MIN
    e = kaelteschutz_entscheiden(jetzt, ist={1: Messwert(8.0, jetzt), 2: Messwert(7.0, jetzt)}, soll=None, delta=None,
                                 zustand=e.zustand, arrival_threshold_k=ANKUNFT_K)
    assert e.art == "aus"


def test_ohne_soll_zaehlt_ein_ungueltiger_raum_nicht_mit_aber_ohne_gueltigen_raum_keine_ankunft():
    e = laufender_frost()
    jetzt = T0 + 20 * MIN
    ohne_zweiten = kaelteschutz_entscheiden(
        jetzt, ist={1: Messwert(9.0, jetzt), 2: Messwert(None, jetzt)}, soll=None, delta=None, zustand=e.zustand,
        arrival_threshold_k=ANKUNFT_K)
    assert ohne_zweiten.art == "aus"
    keiner = kaelteschutz_entscheiden(jetzt, ist={1: Messwert(None, jetzt)}, soll=None, delta=None, zustand=e.zustand,
                                      arrival_threshold_k=ANKUNFT_K)
    assert keiner.art == "frost"


@pytest.mark.parametrize("delta, endet", [(0.6, False), (0.5, True), (0.0, True), (-1.0, True)])
def test_mit_soll_endet_der_lauf_bei_ist_ab_soll_minus_ankunftsschwelle(delta, endet):
    e = laufender_frost(soll=21.0, delta=3.0)
    jetzt = T0 + 20 * MIN
    e = kaelteschutz_entscheiden(jetzt, ist=ist(21.0 - delta, jetzt), soll=21.0, delta=delta, zustand=e.zustand,
                                 arrival_threshold_k=ANKUNFT_K)
    assert (e.art == "aus") is endet


def test_mit_soll_aber_ohne_delta_keine_ankunft():
    e = laufender_frost(soll=21.0, delta=3.0)
    jetzt = T0 + 20 * MIN
    e = kaelteschutz_entscheiden(jetzt, ist=ist(30.0, jetzt), soll=21.0, delta=None, zustand=e.zustand,
                                 arrival_threshold_k=ANKUNFT_K)
    assert e.art == "frost"


# --- Uhr, Reinheit ------------------------------------------------------------------------------------------------

def test_springt_die_uhr_zurueck_endet_der_lauf():
    e = laufender_soll()
    jetzt = stunden(3) - MIN
    e = kaelteschutz_entscheiden(jetzt, ist=ist(18.5, jetzt), soll=21.0, delta=2.5, zustand=e.zustand,
                                 arrival_threshold_k=ANKUNFT_K)
    assert (e.art, e.grund) == ("aus", "uhr_zurueck")


def test_springt_die_uhr_zurueck_beginnt_die_zaehlung_von_vorn():
    zustand = lauf(T0, stunden(1), soll_eingang(2.5))[0].zustand
    jetzt = T0 - 5 * MIN
    e = kaelteschutz_entscheiden(jetzt, ist=ist(18.5, jetzt), soll=21.0, delta=2.5, zustand=zustand,
                                 arrival_threshold_k=ANKUNFT_K)
    assert e.art == "aus"
    assert e.zustand.soll_seit in (None, jetzt)


def test_die_entscheidung_ist_rein():
    zustand = lauf(T0, T0 + 10 * MIN, frost_eingang(4.0))[0].zustand
    eingaben = dict(jetzt=T0 + 15 * MIN, ist={1: Messwert(4.0, T0 + 15 * MIN), 2: Messwert(20.0, T0 + 15 * MIN)},
                    soll=21.0, delta=1.0, zustand=zustand, arrival_threshold_k=ANKUNFT_K)
    kopie = copy.deepcopy(eingaben)
    erste = kaelteschutz_entscheiden(**eingaben)
    zweite = kaelteschutz_entscheiden(**eingaben)
    assert erste == zweite
    assert eingaben == kopie


def test_die_entscheidung_liest_keine_uhr():
    quelle = (emergency_boost.__file__)
    with open(quelle, encoding="utf-8") as f:
        text = f.read()
    for verboten in (".now(", ".utcnow(", "time.time", "monotonic", "sleep("):
        assert verboten not in text, verboten


def test_der_zustand_ist_unveraenderlich():
    with pytest.raises(FrozenInstanceError):
        KaelteschutzZustand().soll_seit = T0  # type: ignore[misc]


# --- Eigenschaft: nie Ausloesung aus einem ungueltigen Wert --------------------------------------------------------

ungueltige_werte = st.one_of(
    st.none(), st.just(math.nan), st.just(math.inf), st.just(-math.inf),
    st.floats(max_value=-10.01, allow_nan=False, allow_infinity=False),
    st.floats(min_value=35.01, allow_nan=False, allow_infinity=False),
)
ungueltige_zeit = st.one_of(st.none(), st.integers(min_value=16 * 60, max_value=10 * 86400).map(lambda s: -timedelta(seconds=s)),
                            st.integers(min_value=1, max_value=86400).map(lambda s: timedelta(seconds=s)))


@st.composite
def ungueltiger_messwert(draw, jetzt):
    if draw(st.booleans()):
        return Messwert(draw(ungueltige_werte), jetzt + (draw(st.sampled_from([timedelta(0), -5 * MIN]))))
    versatz = draw(ungueltige_zeit)
    # gueltiger Wert, aber Zeitstempel in der Zukunft oder laenger als 15 min her bzw. fehlend
    return Messwert(draw(st.floats(min_value=-10, max_value=4.9)), None if versatz is None else jetzt + versatz)


@given(st.data())
def test_eigenschaft_nie_ausloesung_aus_ungueltigem_wert(data):
    zustand = KaelteschutzZustand()
    jetzt = T0
    for _ in range(data.draw(st.integers(min_value=1, max_value=80))):
        jetzt += data.draw(st.sampled_from([MIN, SCHRITT, 20 * MIN, timedelta(hours=1)]))
        ist_je_raum = {r: data.draw(ungueltiger_messwert(jetzt)) for r in (1, 2, 3)}
        delta = data.draw(st.sampled_from([None, math.nan, math.inf]))
        soll = data.draw(st.sampled_from([None, 21.0]))
        e = kaelteschutz_entscheiden(jetzt, ist=ist_je_raum, soll=soll, delta=delta, zustand=zustand,
                                     arrival_threshold_k=ANKUNFT_K)
        assert e.art == "aus"
        zustand = e.zustand


@given(st.lists(st.tuples(st.sampled_from([0, 5, 10, 30]), st.one_of(st.none(), st.floats(-20, 40)),
                          st.sampled_from([None, 0.0, 2.0, 5.0])), min_size=1, max_size=60))
def test_eigenschaft_ein_start_braucht_durchgehend_gueltige_ausloesende_werte(schritte):
    """Wer startet, hatte in der Zaehlung nur gueltige Werte unter 5 Grad bzw. eine Abweichung ab 2 K und keine
    Luecke ueber 15 min: der Zustand vor dem Start zeigt einen Beginn, der mindestens die Dauer zurueckliegt."""
    zustand, jetzt = KaelteschutzZustand(), T0
    for minuten, wert, delta in schritte:
        jetzt += minuten * MIN + MIN
        vorher = zustand
        e = kaelteschutz_entscheiden(jetzt, ist=ist(wert, jetzt), soll=21.0, delta=delta, zustand=zustand,
                                     arrival_threshold_k=ANKUNFT_K)
        if e.gestartet:
            gueltig = wert is not None and -10.0 <= wert <= 35.0
            frost = gueltig and wert < 5.0
            soll_ok = delta is not None and delta >= 2.0
            assert frost or soll_ok
            if e.art == "frost":
                assert frost and jetzt - dict(vorher.frost_seit)[1] >= timedelta(minutes=15)
            else:
                assert soll_ok and jetzt - vorher.soll_seit >= timedelta(hours=3)
        zustand = e.zustand


# --- Randfaelle, die Mutationen zeigten -------------------------------------------------------------------------

def test_zustand_mit_lauf_art_aber_ohne_beginn_zaehlt_wie_ohne_lauf():
    zustand = KaelteschutzZustand(lauf_art="soll", lauf_seit=None)
    e = kaelteschutz_entscheiden(T0, ist=ist(20.0, T0), soll=21.0, delta=0.0, zustand=zustand,
                                 arrival_threshold_k=ANKUNFT_K)
    assert (e.art, e.grund) == ("aus", "kein_ausloeser")


def test_zweite_pruefung_im_selben_augenblick_beendet_den_lauf_nicht():
    e = laufender_soll()
    e = kaelteschutz_entscheiden(stunden(3), ist=ist(18.5, stunden(3)), soll=21.0, delta=2.5, zustand=e.zustand,
                                 arrival_threshold_k=ANKUNFT_K)
    assert (e.art, e.grund) == ("soll", "laeuft")


def test_zweite_pruefung_im_selben_augenblick_setzt_die_zaehlung_fort():
    zustand = KaelteschutzZustand()
    for minute in (0, 5, 5):
        jetzt = T0 + minute * MIN
        zustand = kaelteschutz_entscheiden(jetzt, ist=ist(4.0, jetzt), soll=None, delta=None, zustand=zustand,
                                           arrival_threshold_k=ANKUNFT_K).zustand
    spaet = T0 + 15 * MIN
    e = kaelteschutz_entscheiden(spaet, ist=ist(4.0, spaet), soll=None, delta=None, zustand=zustand,
                                 arrival_threshold_k=ANKUNFT_K)
    assert e.art == "frost"


def test_ein_ende_meldet_keinen_start():
    e = laufender_soll()
    enden = [
        kaelteschutz_entscheiden(stunden(3, 5), ist=ist(20.5, stunden(3, 5)), soll=21.0, delta=0.5, zustand=e.zustand,
                                 arrival_threshold_k=ANKUNFT_K),
        kaelteschutz_entscheiden(stunden(7), ist=ist(18.5, stunden(7)), soll=21.0, delta=2.5, zustand=e.zustand,
                                 arrival_threshold_k=ANKUNFT_K),
        kaelteschutz_entscheiden(stunden(2), ist=ist(18.5, stunden(2)), soll=21.0, delta=2.5, zustand=e.zustand,
                                 arrival_threshold_k=ANKUNFT_K),
    ]
    assert [x.grund for x in enden] == ["ankunft", "hoechstdauer", "uhr_zurueck"]
    assert [x.gestartet for x in enden] == [False, False, False]
