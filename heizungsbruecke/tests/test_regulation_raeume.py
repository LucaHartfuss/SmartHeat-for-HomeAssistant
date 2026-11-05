"""Lokaler Check mit mehreren Raeumen: Boost und Notfall-Boost rechnen mit Soll minus Delta der Referenzraeume
(Ziel je Raum aus Soll und Versatz), Kontrollraeume gehen nie ein, ohne gueltigen Referenzraum zaehlt der Check wie
ein nicht lesbarer Raum."""
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from bindung_beispiele import VAILLANT_MYPYLLANT
from fakes import runtime_config
from hypothesis import given
from hypothesis import strategies as st

from heizungsbruecke.ha_api import HomeAssistantApi
from heizungsbruecke.ha_binding import HaPlantBinding
from smartheat_core import raum_delta
from smartheat_core.pipeline import LeverPipeline
from smartheat_core.safety import LocalSafety
from smartheat_runtime import regulation
from smartheat_runtime.plausibility import ROOM_TEMP_RANGE
from smartheat_runtime.ports import SignalUngueltig
from smartheat_runtime.roles import ChannelManifest

SAFETY = LocalSafety(
    ranges={"curve": (0.2, 0.8), "room_setpoint": (0.0, 5.0), "heat_limit": (5.0, 20.0), "min_flow": (20.0, 30.0)},
    comfort_boost={"curve": 0.5, "room_setpoint": 2.0, "heat_limit": 20.0},
    emergency_boost_levers=("curve", "room_setpoint", "heat_limit"),
    arrival_threshold_k=0.5,
)
REFS = {"room_actual": "sensor.mittel", "room_target": "sensor.soll", "curve_current": "number.curve",
        "shift_current": "number.shift"}
RESTORE_POINT = {"restore_point": {"curve": 0.3, "room_setpoint": 1.0}}
COMFORT = [("number.curve", 0.5), ("number.shift", 2.0)]
EMERGENCY = [("number.curve", 0.8), ("number.shift", 5.0)]


def _rt(make_store, ist, *, referenz=((1, 0.0),), kontroll=(), soll=21.0, vorher=20.0, mittel=15.0, failsafe=None):
    """`ist` Raum -> Wert (eine Exception wird beim Lesen geworfen); `referenz` (Raum, Versatz); `kontroll` Raumnummern;
    `mittel` der Wert des Kreis-Sensors `room_actual` (soll bei Raeumen nie gelesen werden)."""
    raum_refs = {raum: f"sensor.raum{raum}" for raum in ist}
    werte = {"sensor.mittel": mittel, **{raum_refs[raum]: wert for raum, wert in ist.items()}}
    ha_api = MagicMock(spec=HomeAssistantApi)
    ha_api.get_attribute.side_effect = ValueError("kein Wertebereich")

    def _get_state(ref):
        wert = werte[ref]
        if isinstance(wert, Exception):
            raise wert
        return wert

    ha_api.get_state.side_effect = _get_state
    manifest = ChannelManifest(refs=REFS, raum_refs=raum_refs)
    store = make_store(backup={"last_room_target": vorher, **RESTORE_POINT},
                       failsafe=failsafe)
    store.update(stable_target=soll)
    return SimpleNamespace(
        manifest=manifest, signals=ha_api, ha_api=ha_api, store=store, clock=time.monotonic, boost_mono_start=None,
        config=runtime_config(referenzraeume=tuple(referenz)),
        override=LeverPipeline(store, HaPlantBinding(ha_api, manifest, VAILLANT_MYPYLLANT), SAFETY),
    )


def _writes(rt):
    return [call.args for call in rt.ha_api.set_number_value.call_args_list]


@pytest.mark.parametrize("ist,boost", [(20.5, False), (20.49, True), (19.0, True), (21.0, False)])
def test_der_boost_schwelle_liegt_bei_einem_delta_von_einem_halben_kelvin(make_store, ist, boost):
    rt = _rt(make_store, {1: ist})

    regulation.run_local_check(rt)

    assert rt.store.state.boost_active is boost
    assert _writes(rt) == (COMFORT if boost else [])


def test_ein_referenzraum_liest_seinen_eigenen_fuehler_und_nicht_den_kreismittelwert(make_store):
    rt = _rt(make_store, {1: 19.0}, mittel=21.0)  # der Mittelwert-Sensor sagt warm, der Raum ist kalt

    regulation.run_local_check(rt)

    assert rt.store.state.boost_active is True
    assert "sensor.mittel" not in [call.args[0] for call in rt.ha_api.get_state.call_args_list]


def test_ein_positiver_versatz_hebt_das_ziel_und_startet_den_boost_frueher(make_store):
    # Ziel 22, Ist 21.4: Delta 0.6 > 0.5 -> Boost; ohne Versatz waere es Delta -0.4
    mit = _rt(make_store, {1: 21.4}, referenz=((1, 1.0),))
    regulation.run_local_check(mit)
    assert mit.store.state.boost_active is True


def test_ohne_versatz_startet_derselbe_ist_wert_keinen_boost(make_store):
    ohne = _rt(make_store, {1: 21.4}, referenz=((1, 0.0),))
    regulation.run_local_check(ohne)
    assert ohne.store.state.boost_active is False


def test_ein_negativer_versatz_senkt_das_ziel(make_store):
    # Ziel 20 bei Soll 21 und Versatz -1: Ist 19.6 -> Delta 0.4 -> kein Boost (ohne Versatz 1.4 -> Boost)
    rt = _rt(make_store, {1: 19.6}, referenz=((1, -1.0),))

    regulation.run_local_check(rt)

    assert rt.store.state.boost_active is False


def test_zwei_referenzraeume_mitteln_ihr_delta(make_store):
    # Raum 1 Delta 1.0, Raum 2 Delta 0.0 -> Mittel 0.5: gerade noch kein Boost; Raum 2 minimal kaelter -> Boost
    gerade = _rt(make_store, {1: 20.0, 2: 21.0}, referenz=((1, 0.0), (2, 0.0)))
    regulation.run_local_check(gerade)
    assert gerade.store.state.boost_active is False


def test_zwei_referenzraeume_mit_minimal_kaelterem_zweiten_starten_den_boost(make_store):
    rt = _rt(make_store, {1: 20.0, 2: 20.98}, referenz=((1, 0.0), (2, 0.0)))

    regulation.run_local_check(rt)

    assert rt.store.state.boost_active is True


def test_ein_kontrollraum_aendert_die_entscheidung_nie(make_store):
    warm = _rt(make_store, {1: 20.8, 3: 35.0}, referenz=((1, 0.0),))
    regulation.run_local_check(warm)
    assert warm.store.state.boost_active is False  # der heisse Kontrollraum haelt nichts zurueck
    assert "sensor.raum3" not in [call.args[0] for call in warm.ha_api.get_state.call_args_list]


def test_ein_kalter_kontrollraum_startet_keinen_boost(make_store):
    kalt = _rt(make_store, {1: 20.8, 3: 5.0}, referenz=((1, 0.0),))

    regulation.run_local_check(kalt)

    assert kalt.store.state.boost_active is False and _writes(kalt) == []


def test_der_notfall_boost_rechnet_mit_dem_delta(make_store):
    rt = _rt(make_store, {1: 19.9}, soll=21.0, failsafe={"failsafe_active": True})

    regulation.run_local_check(rt)

    assert _writes(rt) == EMERGENCY and rt.store.state.emergency_boost_active is True


@pytest.mark.parametrize("ist", [4.9, 0.0, -10.0])
def test_ein_ist_unter_5_grad_loest_im_notbetrieb_den_notfall_boost_aus(make_store, ist):
    rt = _rt(make_store, {1: ist}, soll=21.0, failsafe={"failsafe_active": True})

    regulation.run_local_check(rt)

    assert _writes(rt) == EMERGENCY and rt.store.state.emergency_boost_active is True


def test_ein_ist_unter_minus_10_grad_ist_ungueltig_und_loest_nichts_aus(make_store):
    rt = _rt(make_store, {1: -10.1}, soll=21.0, failsafe={"failsafe_active": True})

    with pytest.raises(SignalUngueltig):
        regulation.run_local_check(rt)

    assert _writes(rt) == [] and rt.store.state.emergency_boost_active is False


def test_ein_ungueltiger_referenzraum_faellt_aus_dem_mittel(make_store):
    # Raum 2 liefert Unsinn (ausserhalb des Bereichs): nur Raum 1 zaehlt, Delta 1.0 -> Boost
    rt = _rt(make_store, {1: 20.0, 2: 99.0}, referenz=((1, 0.0), (2, 0.0)))

    regulation.run_local_check(rt)

    assert rt.store.state.boost_active is True


def test_ein_nicht_lesbarer_referenzraum_faellt_aus_dem_mittel(make_store):
    rt = _rt(make_store, {1: 20.0, 2: SignalUngueltig("unavailable")}, referenz=((1, 0.0), (2, 0.0)))

    regulation.run_local_check(rt)

    assert rt.store.state.boost_active is True and rt.store.state.room_actual_misses == 0


def test_ohne_gueltigen_referenzraum_zaehlt_der_check_wie_ein_nicht_lesbarer_raum(make_store):
    # der Kontrollraum ist warm und gueltig, hilft aber nicht
    rt = _rt(make_store, {1: SignalUngueltig("unavailable"), 3: 21.0}, referenz=((1, 0.0),))

    with pytest.raises(SignalUngueltig):
        regulation.run_local_check(rt)

    assert rt.store.state.room_actual_misses == 1 and _writes(rt) == []


def test_ein_anderer_fehler_beim_lesen_eines_referenzraums_ist_kein_datenfehler(make_store):
    rt = _rt(make_store, {1: 20.0, 2: RuntimeError("HA-API-Hickup")}, referenz=((1, 0.0), (2, 0.0)))

    with pytest.raises(RuntimeError):
        regulation.run_local_check(rt)

    assert _writes(rt) == []


def test_drei_checks_ohne_gueltigen_referenzraum_beenden_den_laufenden_boost(make_store):
    rt = _rt(make_store, {1: SignalUngueltig("unavailable")}, referenz=((1, 0.0),))
    rt.store.update(boost_active=True)

    for _ in range(3):
        with pytest.raises(SignalUngueltig):
            regulation.run_local_check(rt)

    assert rt.store.state.boost_active is False


def test_ein_lesbarer_referenzraum_setzt_den_zaehler_zurueck(make_store):
    rt = _rt(make_store, {1: 20.0})
    rt.store.update(room_actual_misses=2)

    regulation.run_local_check(rt)

    assert rt.store.state.room_actual_misses == 0


def test_ohne_referenzraeume_in_der_konfiguration_gilt_der_kreissensor(make_store):
    rt = _rt(make_store, {}, referenz=(), mittel=19.0)

    regulation.run_local_check(rt)

    assert rt.store.state.boost_active is True


# --- wirksames Raum-Ist: Kontrollraeume ändern nie etwas (Eigenschaft) ---

def _lese(rt_ist, referenz, kontroll_ist, soll):
    """wirksames_raum_ist ohne Store: die Raeume 1.. sind Referenz, 100.. Kontrolle."""
    raum_refs = {**{raum: f"r{raum}" for raum in rt_ist}, **{100 + i: f"k{i}" for i in range(len(kontroll_ist))}}
    werte = {**{f"r{raum}": wert for raum, wert in rt_ist.items()}, **{f"k{i}": w for i, w in enumerate(kontroll_ist)}}
    signals = SimpleNamespace(get_state=werte.__getitem__)
    rt = SimpleNamespace(manifest=SimpleNamespace(refs={}, raum_refs=raum_refs), signals=signals,
                         config=SimpleNamespace(referenzraeume=tuple(referenz)), store=None)
    return regulation.wirksames_raum_ist(rt, soll)


@given(
    ist=st.dictionaries(st.integers(1, 4), st.floats(*ROOM_TEMP_RANGE), min_size=1, max_size=4),
    versatz=st.lists(st.sampled_from([-3.0, -1.5, 0.0, 0.5, 3.0]), min_size=4, max_size=4),
    kontroll=st.lists(st.floats(-100, 100), max_size=4),
    soll=st.floats(15.0, 25.0),
)
def test_kontrollraeume_aendern_das_wirksame_ist_nie(ist, versatz, kontroll, soll):
    referenz = tuple((raum, versatz[raum - 1]) for raum in ist)
    ohne = _lese(ist, referenz, [], soll)
    mit = _lese(ist, referenz, kontroll, soll)
    assert ohne == mit
    erwartet = raum_delta.wirksames_ist(soll, ist, dict(referenz))
    assert mit == erwartet


def test_wirksames_ist_gibt_bei_einem_referenzraum_ohne_versatz_den_fuehlerwert_unveraendert_zurueck():
    assert _lese({1: 20.123456789}, ((1, 0.0),), [], 23.7) == 20.123456789
