"""Kaelteschutz im lokalen Check (regulation.py): Start nach Abweichung oder Frostnaehe, Ende bei Ankunft oder nach der
Hoechstdauer, Start-Zaehler des Notfall-Boosts, Tageslimit, Neustart, Abo-Zustaende, Notbetrieb. Echter StateStore und
echte Hebel-Pipeline; die Entscheidung selbst steht in test_core_kaelteschutz.py."""
import contextlib
import dataclasses
import logging
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from bindung_beispiele import VAILLANT_MYPYLLANT
from fakes import FakeClock, WallClock, runtime_config

from heizungsbruecke.ha_api import HomeAssistantApi
from heizungsbruecke.ha_binding import HaPlantBinding
from smartheat_core import wallclock, write_budget
from smartheat_core.emergency_boost import KaelteschutzZustand
from smartheat_core.pipeline import LeverPipeline
from smartheat_core.safety import LocalSafety
from smartheat_runtime import regulation
from smartheat_runtime.backup_store import save_backup
from smartheat_runtime.delivery import DeliveryState
from smartheat_runtime.ports import SignalUngueltig
from smartheat_runtime.roles import ChannelManifest
from smartheat_runtime.state import StateStore

SAFETY = LocalSafety(
    ranges={"curve": (0.2, 0.8), "room_setpoint": (0.0, 5.0), "heat_limit": (5.0, 20.0), "min_flow": (20.0, 30.0)},
    comfort_boost={"curve": 0.5, "room_setpoint": 2.0, "heat_limit": 20.0},
    emergency_boost_levers=("curve", "room_setpoint", "heat_limit"),
    arrival_threshold_k=0.5,
)
REFS = {"room_actual": "sensor.mittel", "room_target": "sensor.soll", "curve_current": "number.curve",
        "shift_current": "number.shift"}
RESTORE_POINT = {"restore_point": {"curve": 0.3, "room_setpoint": 1.0}}
EMERGENCY = [("number.curve", 0.8), ("number.shift", 5.0)]
RESTORE = [("number.curve", 0.3), ("number.shift", 1.0)]
BERLIN = ZoneInfo("Europe/Berlin")
START = datetime(2026, 10, 3, 12, 0, tzinfo=BERLIN)
DAY = "2026-10-03"
STUNDE = 3600
MINUTE = 60


@pytest.fixture
def wall(monkeypatch):
    uhr = WallClock(START)
    monkeypatch.setattr(wallclock, "_now", uhr.now)
    return uhr


def _store_in(verzeichnis: Path, backup: dict | None = None) -> StateStore:
    verzeichnis.mkdir(parents=True, exist_ok=True)
    if backup is not None:
        save_backup(verzeichnis / "backup.json", backup)
    return StateStore(verzeichnis / "backup.json", verzeichnis / "failsafe_state.json")


def _kreis(store, wall, clock, ist, *, soll=21.0, referenz=((1, 0.0),), beschreibung=VAILLANT_MYPYLLANT,
           notbetrieb=False):
    """Ein Kreis: `ist` Raumnummer -> Wert (eine Exception wird beim Lesen geworfen); `referenz` (Raum, Versatz), die
    uebrigen Raeume sind Kontrollraeume; `soll` None = kein Soll."""
    raum_refs = {raum: f"sensor.raum{raum}" for raum in ist}
    werte = {"sensor.mittel": 21.0, **{raum_refs[raum]: wert for raum, wert in ist.items()}}
    ha_api = MagicMock(spec=HomeAssistantApi)
    ha_api.get_attribute.side_effect = ValueError("kein Wertebereich")

    def _get_state(ref):
        wert = werte[ref]
        if isinstance(wert, Exception):
            raise wert
        return wert

    ha_api.get_state.side_effect = _get_state
    manifest = ChannelManifest(refs=REFS, raum_refs=raum_refs)
    if soll is not None:
        store.update(stable_target=soll)
    if notbetrieb:
        store.set_delivery(DeliveryState(notbetrieb=True))
    return SimpleNamespace(
        manifest=manifest, signals=ha_api, ha_api=ha_api, store=store, clock=clock, boost_mono_start=None,
        soll_quelle_aus=False,
        config=runtime_config(referenzraeume=tuple(referenz)), werte=werte, raum_refs=raum_refs, wand=wall,
        override=LeverPipeline(store, HaPlantBinding(ha_api, manifest, beschreibung), SAFETY, clock=clock),
    )


def _neu(make_store, wall, clock, ist, *, soll=21.0, backup=None, **optionen):
    start = {"restore_point": RESTORE_POINT["restore_point"], **(backup or {})}
    if soll is not None:
        start["last_room_target"] = soll
    return _kreis(make_store(backup=start), wall, clock, ist, soll=soll, **optionen)


def _ist(rt, **werte):
    """Setzt die Werte der Raeume: _ist(rt, r1=18.0)."""
    for name, wert in werte.items():
        rt.werte[rt.raum_refs[int(name[1:])]] = wert


def _pruefen(rt):
    with contextlib.suppress(SignalUngueltig):
        regulation.run_local_check(rt)


def _lauf(rt, sekunden, schritt=5 * MINUTE):
    """Prueft im Takt `schritt`, bis `sekunden` vergangen sind; die letzte Pruefung liegt genau dort."""
    rest = sekunden
    while rest > 0:
        teil = min(schritt, rest)
        rt.wand.advance(teil)
        rt.clock.advance(teil)
        rest -= teil
        _pruefen(rt)


def _writes(rt):
    return [call.args for call in rt.ha_api.set_number_value.call_args_list]


def _starts(rt):
    return write_budget.count_today(write_budget.get(rt.store, write_budget.EMERGENCY_START), DAY)


def _lauft(rt):
    return rt.store.state.emergency_boost_active


@pytest.fixture
def uhr():
    return FakeClock(time.monotonic())


# --- Soll-Ausloeser ----------------------------------------------------------------------------------------------

def test_die_abweichung_loest_nach_drei_stunden_aus_und_nicht_vorher(make_store, wall, uhr):
    rt = _neu(make_store, wall, uhr, {1: 18.0})
    _pruefen(rt)

    _lauf(rt, 3 * STUNDE - MINUTE)

    assert not _lauft(rt) and _writes(rt) == [] and rt.store.state.kaelteschutz_art is None

    _lauf(rt, MINUTE)

    assert _lauft(rt) and _writes(rt) == EMERGENCY
    assert rt.store.state.kaelteschutz_art == "soll" and _starts(rt) == 1
    assert rt.store.state.kaelteschutz_seit == wall.now().isoformat()
    assert rt.store.state.boost_active is False


def test_eine_abweichung_unter_zwei_kelvin_loest_nie_aus(make_store, wall, uhr):
    rt = _neu(make_store, wall, uhr, {1: 19.1})  # 1,9 K unter dem Ziel

    _pruefen(rt)
    _lauf(rt, 6 * STUNDE)

    assert not _lauft(rt) and _writes(rt) == []


def test_nur_die_referenzraeume_bestimmen_die_abweichung_mit_dem_versatz_je_raum(make_store, wall, uhr):
    # Raum 1 (Versatz 0) ist warm, Raum 2 (Versatz +1, Ziel 22) 1,5 K darunter: Mittel 0,75 K; Kontrollraum 3 geht nie ein
    rt = _neu(make_store, wall, uhr, {1: 21.0, 2: 20.5, 3: 15.0}, referenz=((1, 0.0), (2, 1.0)))
    _pruefen(rt)

    _lauf(rt, 4 * STUNDE)

    assert not _lauft(rt)

    _ist(rt, r2=18.0)  # Raum 2: 4 K darunter, Mittel 2 K

    _lauf(rt, 3 * STUNDE)  # die Zaehlung beginnt mit dem ersten Check nach der Aenderung

    assert not _lauft(rt)

    _lauf(rt, 5 * MINUTE)

    assert _lauft(rt) and rt.store.state.kaelteschutz_art == "soll"


# --- Frostnaehe --------------------------------------------------------------------------------------------------

def test_die_frostnaehe_loest_nach_fuenfzehn_minuten_aus_und_nicht_vorher(make_store, wall, uhr):
    rt = _neu(make_store, wall, uhr, {1: 4.0})
    _pruefen(rt)

    _lauf(rt, 15 * MINUTE - 1, schritt=MINUTE)

    assert not _lauft(rt) and _writes(rt) == []

    _lauf(rt, 1, schritt=MINUTE)

    assert _lauft(rt) and _writes(rt) == EMERGENCY and rt.store.state.kaelteschutz_art == "frost"


def test_ein_kontrollraum_in_frostnaehe_loest_aus_obwohl_der_referenzraum_warm_ist(make_store, wall, uhr):
    rt = _neu(make_store, wall, uhr, {1: 21.0, 2: 4.9})
    _pruefen(rt)

    _lauf(rt, 15 * MINUTE, schritt=MINUTE)

    assert _lauft(rt) and rt.store.state.kaelteschutz_art == "frost"


def test_ein_frost_lauf_mit_soll_haelt_bei_warmem_referenzraum_bis_alle_raeume_sieben_grad_haben(make_store, wall, uhr):
    rt = _neu(make_store, wall, uhr, {1: 21.0, 2: 4.9})
    _pruefen(rt)
    _lauf(rt, 15 * MINUTE, schritt=MINUTE)
    assert _lauft(rt) and rt.store.state.kaelteschutz_art == "frost" and _starts(rt) == 1

    _lauf(rt, 30 * MINUTE)  # sechs Checks mit warmem Referenzraum (Abweichung 0): kein Ende, kein neuer Start

    assert _lauft(rt) and rt.store.state.kaelteschutz_art == "frost"
    assert _starts(rt) == 1 and _writes(rt) == EMERGENCY

    _ist(rt, r2=6.9)
    _lauf(rt, 5 * MINUTE)

    assert _lauft(rt) and _writes(rt) == EMERGENCY

    _ist(rt, r2=7.0)
    _lauf(rt, 5 * MINUTE)

    assert not _lauft(rt) and _writes(rt) == EMERGENCY + RESTORE and _starts(rt) == 1


def test_genau_fuenf_grad_ist_keine_frostnaehe(make_store, wall, uhr):
    rt = _neu(make_store, wall, uhr, {1: 5.0, 2: 21.0}, soll=None)

    _pruefen(rt)
    _lauf(rt, 2 * STUNDE)

    assert not _lauft(rt)


# --- Fuehlerfehler und Luecken -----------------------------------------------------------------------------------

@pytest.mark.parametrize("wert", [SignalUngueltig("kaputt"), 50.0, -10.5, float("nan")],
                         ids=["unlesbar", "ueber_35", "unter_minus_10", "nan"])
def test_ein_fuehlerfehler_loest_nichts_aus(make_store, wall, uhr, wert):
    rt = _neu(make_store, wall, uhr, {1: wert})

    _pruefen(rt)
    _lauf(rt, 5 * STUNDE)

    assert not _lauft(rt) and _writes(rt) == [] and rt.store.state.kaelteschutz_art is None


def test_eine_luecke_ueber_fuenfzehn_minuten_beginnt_die_zaehlung_von_vorn(make_store, wall, uhr):
    rt = _neu(make_store, wall, uhr, {1: 18.0})
    _pruefen(rt)
    _lauf(rt, 2 * STUNDE)
    _ist(rt, r1=SignalUngueltig("weg"))
    _lauf(rt, 20 * MINUTE)
    _ist(rt, r1=18.0)

    _lauf(rt, 3 * STUNDE)  # insgesamt weit ueber drei Stunden, aber der erste Check nach der Luecke zaehlt erst ab 5 min

    assert not _lauft(rt)

    _lauf(rt, 5 * MINUTE)

    assert _lauft(rt)


def test_ein_unlesbarer_raum_zaehlt_weiter_als_unlesbar(make_store, wall, uhr):
    """Der Kaelteschutz verschluckt keinen Lesefehler: der Check wirft wie bisher und zaehlt den Fehlversuch."""
    rt = _neu(make_store, wall, uhr, {1: SignalUngueltig("weg")})

    with pytest.raises(SignalUngueltig):
        regulation.run_local_check(rt)

    assert rt.store.state.room_actual_misses == 1


# --- Ende des Laufs ----------------------------------------------------------------------------------------------

def _gestartet(make_store, wall, uhr, ist=18.0, **optionen):
    rt = _neu(make_store, wall, uhr, {1: ist}, **optionen)
    _pruefen(rt)
    _lauf(rt, 3 * STUNDE)
    assert _lauft(rt)
    return rt


def test_der_lauf_endet_bei_ankunft_und_nicht_knapp_davor(make_store, wall, uhr):
    rt = _gestartet(make_store, wall, uhr)

    _ist(rt, r1=20.4)  # 0,6 K unter dem Soll
    _lauf(rt, 5 * MINUTE)

    assert _lauft(rt) and _writes(rt) == EMERGENCY

    _ist(rt, r1=20.5)
    _lauf(rt, 5 * MINUTE)

    assert not _lauft(rt) and _writes(rt) == EMERGENCY + RESTORE
    assert rt.store.state.kaelteschutz_art is None and rt.store.state.kaelteschutz_seit is None
    assert rt.store.state.kaelteschutz_zaehlung.lauf_art is None


def test_der_lauf_endet_nach_vier_stunden(make_store, wall, uhr):
    rt = _gestartet(make_store, wall, uhr)

    _lauf(rt, 4 * STUNDE - MINUTE, schritt=10 * MINUTE)

    assert _lauft(rt)

    _lauf(rt, MINUTE)

    assert not _lauft(rt) and _writes(rt) == EMERGENCY + RESTORE and rt.store.state.kaelteschutz_art is None


def test_der_ausfall_des_fuehlers_beendet_einen_laufenden_lauf_nicht(make_store, wall, uhr):
    rt = _gestartet(make_store, wall, uhr)
    _ist(rt, r1=SignalUngueltig("weg"))

    _lauf(rt, 2 * STUNDE)

    assert _lauft(rt) and _writes(rt) == EMERGENCY and rt.store.state.kaelteschutz_art == "soll"


def test_nach_dem_ende_loest_der_kaelteschutz_erneut_aus_aber_erst_nach_der_vollen_dauer(make_store, wall, uhr):
    rt = _gestartet(make_store, wall, uhr)
    _ist(rt, r1=21.0)
    _lauf(rt, 5 * MINUTE)
    assert not _lauft(rt)
    _ist(rt, r1=18.0)

    _lauf(rt, 3 * STUNDE)  # die Zaehlung beginnt neu, auch wenn der alte Lauf zuvor weit ueber drei Stunden lief

    assert not _lauft(rt)

    _lauf(rt, 5 * MINUTE)

    assert _lauft(rt) and _writes(rt) == EMERGENCY + RESTORE + EMERGENCY and _starts(rt) == 2


def test_ohne_soll_endet_der_lauf_erst_ab_sieben_grad(make_store, wall, uhr):
    rt = _neu(make_store, wall, uhr, {1: 4.0}, soll=None)
    _pruefen(rt)
    _lauf(rt, 15 * MINUTE, schritt=MINUTE)
    assert _lauft(rt)

    _ist(rt, r1=6.9)
    _lauf(rt, 5 * MINUTE)

    assert _lauft(rt)

    _ist(rt, r1=7.0)
    _lauf(rt, 5 * MINUTE)

    assert not _lauft(rt) and _writes(rt) == EMERGENCY + RESTORE


def test_bei_soll_quelle_aus_loest_die_abweichung_vom_alten_wunsch_nicht_aus_und_nach_der_rueckkehr_wieder(
        make_store, wall, uhr):
    rt = _neu(make_store, wall, uhr, {1: 18.0})  # 3 K unter dem letzten Wunsch
    rt.soll_quelle_aus = True  # Thermostat aus oder Frostschutz-Stellung: der alte Wunsch bleibt im Speicher stehen

    _pruefen(rt)
    _lauf(rt, 6 * STUNDE)

    assert not _lauft(rt) and _writes(rt) == [] and _starts(rt) == 0

    rt.soll_quelle_aus = False
    _pruefen(rt)
    _lauf(rt, 3 * STUNDE - MINUTE)
    assert not _lauft(rt)

    _lauf(rt, MINUTE)

    assert _lauft(rt) and rt.store.state.kaelteschutz_art == "soll" and _starts(rt) == 1


def test_bei_soll_quelle_aus_bleibt_die_frostnaehe_wirksam_und_der_lauf_endet_bei_sieben_grad(make_store, wall, uhr):
    rt = _neu(make_store, wall, uhr, {1: 4.0})
    rt.soll_quelle_aus = True

    _pruefen(rt)
    _lauf(rt, 15 * MINUTE, schritt=MINUTE)

    assert _lauft(rt) and rt.store.state.kaelteschutz_art == "frost" and _writes(rt) == EMERGENCY

    _ist(rt, r1=6.9)
    _lauf(rt, 5 * MINUTE)
    assert _lauft(rt)

    _ist(rt, r1=7.0)
    _lauf(rt, 5 * MINUTE)

    assert not _lauft(rt) and _writes(rt) == EMERGENCY + RESTORE


def test_ohne_soll_loest_nur_die_frostnaehe_aus(make_store, wall, uhr, caplog):
    rt = _neu(make_store, wall, uhr, {1: 12.0}, soll=None)

    with caplog.at_level(logging.WARNING):
        _pruefen(rt)
        _lauf(rt, 6 * STUNDE)

    assert not _lauft(rt) and _writes(rt) == []


def test_ohne_raumliste_gilt_der_kreissensor_ohne_versatz_gegen_die_schwelle(make_store, wall, uhr):
    rt = _neu(make_store, wall, uhr, {}, referenz=())
    rt.werte["sensor.mittel"] = 19.1  # 1,9 K unter dem Soll
    _pruefen(rt)

    _lauf(rt, 4 * STUNDE)

    assert not _lauft(rt) and _writes(rt) == []

    rt.werte["sensor.mittel"] = 19.0  # genau 2,0 K
    _lauf(rt, 3 * STUNDE + 5 * MINUTE)

    assert _lauft(rt) and rt.store.state.kaelteschutz_art == "soll"


def test_eine_art_ohne_beginn_im_speicher_gilt_nicht_als_lauf(make_store, wall, uhr):
    rt = _neu(make_store, wall, uhr, {1: 21.0})
    rt.store.update(kaelteschutz_art="soll")

    _pruefen(rt)

    assert rt.store.state.kaelteschutz_art is None and _writes(rt) == [] and not _lauft(rt)


def test_das_ende_wird_einmal_gemeldet_und_ein_ruhiger_check_meldet_nichts(make_store, wall, uhr, caplog):
    rt = _neu(make_store, wall, uhr, {1: 4.0}, soll=None)

    with caplog.at_level(logging.INFO):
        _pruefen(rt)
        assert "Kaelteschutz endet" not in caplog.text
        _lauf(rt, 15 * MINUTE, schritt=MINUTE)
        assert _lauft(rt) and "Kaelteschutz endet" not in caplog.text
        _ist(rt, r1=21.0)
        _lauf(rt, 5 * MINUTE)
        assert caplog.text.count("Kaelteschutz endet") == 1
        _lauf(rt, 5 * MINUTE)

    assert caplog.text.count("Kaelteschutz endet") == 1


# --- Start-Zaehler -----------------------------------------------------------------------------------------------

def test_der_siebte_start_am_tag_wird_abgelehnt_und_am_naechsten_tag_nachgeholt(make_store, wall, uhr):
    eintrag = {"day": DAY, "count": 6}
    rt = _neu(make_store, wall, uhr, {1: 18.0}, backup={"write_budget": {write_budget.EMERGENCY_START: eintrag}})
    _pruefen(rt)

    _lauf(rt, 4 * STUNDE)

    assert not _lauft(rt) and _writes(rt) == [] and rt.store.state.kaelteschutz_art is None
    assert _starts(rt) == 6

    _lauf(rt, 8 * STUNDE - 5 * MINUTE)  # 23:55: Mitternacht liegt 12 h nach dem Beginn
    assert not _lauft(rt)

    _lauf(rt, 5 * MINUTE)  # 00:00 des Folgetags

    assert _lauft(rt) and _writes(rt) == EMERGENCY and rt.store.state.kaelteschutz_art == "soll"


def test_ein_vom_zaehler_abgelehneter_start_wird_beim_naechsten_check_wiederholt(make_store, wall, uhr):
    """Weniger als 30 Minuten seit dem letzten Start: der Lauf ist nicht verbraucht, der erste Check nach Ablauf der
    Wartezeit startet ihn (nicht erst nach weiteren drei Stunden)."""
    rt = _neu(make_store, wall, uhr, {1: 18.0})
    letzter = uhr.now + 3 * STUNDE - 10 * MINUTE
    write_budget.put(rt.store, write_budget.EMERGENCY_START, {"day": DAY, "count": 1, "last": letzter})
    _pruefen(rt)

    _lauf(rt, 3 * STUNDE)

    assert not _lauft(rt) and _writes(rt) == []

    _lauf(rt, 15 * MINUTE)

    assert not _lauft(rt)

    _lauf(rt, 5 * MINUTE)  # 30 Minuten nach dem letzten Start

    assert _lauft(rt) and _writes(rt) == EMERGENCY and _starts(rt) == 2


def test_der_notfall_boost_im_notbetrieb_und_der_kaelteschutz_teilen_den_start_zaehler(make_store, wall, uhr):
    rt = _neu(make_store, wall, uhr, {1: 19.0}, notbetrieb=True)  # 2 K unter dem Soll: Notfall-Boost startet sofort
    _pruefen(rt)

    assert _lauft(rt) and _starts(rt) == 1


# --- Tageslimit der Schreibvorgaenge -----------------------------------------------------------------------------

def _begrenzt():
    return dataclasses.replace(VAILLANT_MYPYLLANT, daily_write_limit=1)


def test_ein_erschoepftes_tageslimit_haelt_den_kaelteschutz_nicht_auf_und_er_zaehlt_mit(make_store, wall, uhr):
    backup = {"write_budget": {write_budget.TOTAL: {"day": DAY, "count": 1}}}
    rt = _neu(make_store, wall, uhr, {1: 18.0}, backup=backup, beschreibung=_begrenzt())
    _pruefen(rt)

    _lauf(rt, 3 * STUNDE)

    assert _lauft(rt) and _writes(rt) == EMERGENCY and rt.store.state.kaelteschutz_art == "soll"
    assert _starts(rt) == 1
    assert write_budget.count_today(write_budget.get(rt.store, write_budget.TOTAL), DAY) > 1

    _ist(rt, r1=21.0)
    _lauf(rt, 5 * MINUTE)

    assert not _lauft(rt) and _writes(rt) == EMERGENCY + RESTORE  # auch der Rueckweg geht am Tageslimit vorbei


def test_der_notfall_boost_im_notbetrieb_bleibt_am_tageslimit_abgelehnt(make_store, wall, uhr):
    backup = {"write_budget": {write_budget.TOTAL: {"day": DAY, "count": 1}}}
    rt = _neu(make_store, wall, uhr, {1: 19.0}, backup=backup, beschreibung=_begrenzt(), notbetrieb=True)

    _pruefen(rt)

    assert not _lauft(rt) and _writes(rt) == []


def test_das_tageslimit_ohne_kaelteschutz_aendert_nichts_am_start_zaehler_des_kaelteschutzes(make_store, wall, uhr):
    """Gegenprobe: ein erschoepfter Start-Zaehler lehnt den Kaelteschutz auch mit freiem Tageslimit ab."""
    backup = {"write_budget": {write_budget.EMERGENCY_START: {"day": DAY, "count": 6}}}
    rt = _neu(make_store, wall, uhr, {1: 18.0}, backup=backup, beschreibung=_begrenzt())
    _pruefen(rt)

    _lauf(rt, 4 * STUNDE)

    assert not _lauft(rt) and _writes(rt) == []


# --- Serverwerte unter dem Lauf ----------------------------------------------------------------------------------

def test_serverwerte_schreibt_das_geraet_unter_dem_lauf_erst_bei_der_ankunft(make_store, wall, uhr):
    rt = _gestartet(make_store, wall, uhr)

    rt.override.apply_server_values({"curve": 0.6, "room_setpoint": 3.0})

    assert _writes(rt) == EMERGENCY  # nichts Neues auf der Anlage

    _ist(rt, r1=21.0)
    _lauf(rt, 5 * MINUTE)

    assert _writes(rt) == EMERGENCY + [("number.curve", 0.6), ("number.shift", 3.0)]


# --- Neustart ----------------------------------------------------------------------------------------------------

def test_ein_neustart_haelt_den_lauf_und_beendet_ihn_bei_ankunft(make_store, wall, uhr):
    rt = _gestartet(make_store, wall, uhr)
    seit = rt.store.state.kaelteschutz_seit

    neu = _kreis(make_store(), wall, uhr, {1: 18.0})

    assert neu.store.state.kaelteschutz_art == "soll" and neu.store.state.kaelteschutz_seit == seit
    assert neu.store.state.kaelteschutz_zaehlung == KaelteschutzZustand()
    _lauf(neu, 10 * MINUTE)
    assert _lauft(neu) and _writes(neu) == []  # der Lauf geht weiter, nichts wird neu gestartet
    _ist(neu, r1=21.0)
    _lauf(neu, 5 * MINUTE)

    assert not _lauft(neu) and _writes(neu) == RESTORE and neu.store.state.kaelteschutz_art is None


def test_ein_neustart_zaehlt_die_ausloeser_neu(make_store, wall, uhr):
    rt = _neu(make_store, wall, uhr, {1: 18.0})
    _pruefen(rt)
    _lauf(rt, 2 * STUNDE + 50 * MINUTE)

    neu = _kreis(make_store(), wall, uhr, {1: 18.0})
    _pruefen(neu)
    _lauf(neu, 2 * STUNDE + 50 * MINUTE)

    assert not _lauft(neu) and not _lauft(rt)

    _lauf(neu, 10 * MINUTE)

    assert _lauft(neu)


def test_ein_altbestand_ohne_die_neuen_felder_laedt_ohne_lauf(make_store):
    store = make_store(backup={"last_room_target": 21.0, "boost_active": False, **RESTORE_POINT})

    assert store.state.kaelteschutz_art is None and store.state.kaelteschutz_seit is None
    assert store.state.kaelteschutz_zaehlung == KaelteschutzZustand()


@pytest.mark.parametrize("backup", [
    {"kaelteschutz_art": "soll", "kaelteschutz_seit": "kein Datum"},
    {"kaelteschutz_art": "unfug", "kaelteschutz_seit": "2026-10-03T12:00:00+02:00"},
    {"kaelteschutz_art": "frost"},
    {"kaelteschutz_seit": "2026-10-03T12:00:00+02:00"},
    {"kaelteschutz_art": 5, "kaelteschutz_seit": 7},
    {"kaelteschutz_art": "soll", "kaelteschutz_seit": "2026-10-03T12:00:00"},
], ids=["datum_unlesbar", "art_unbekannt", "ohne_beginn", "ohne_art", "kein_text", "ohne_zeitzone"])
def test_ein_unvollstaendiger_oder_kaputter_lauf_in_der_datei_gilt_als_kein_lauf(make_store, backup):
    store = make_store(backup={"last_room_target": 21.0, **RESTORE_POINT, **backup})

    assert store.state.kaelteschutz_art is None and store.state.kaelteschutz_seit is None


# --- Abo-Zustaende und Notbetrieb --------------------------------------------------------------------------------

def test_nach_dem_abo_ende_greift_der_kaelteschutz_nicht(make_store, wall, uhr):
    rt = _neu(make_store, wall, uhr, {1: 4.0})
    rt.store.update(abo_finished=True)

    _pruefen(rt)
    _lauf(rt, 5 * STUNDE)

    assert not _lauft(rt) and _writes(rt) == [] and rt.store.state.kaelteschutz_art is None


def test_in_der_kulanzfrist_ohne_abo_greift_der_kaelteschutz(make_store, wall, uhr):
    rt = _neu(make_store, wall, uhr, {1: 18.0})
    rt.store.update(abo_inactive_since=START)
    _pruefen(rt)

    _lauf(rt, 3 * STUNDE)

    assert _lauft(rt) and rt.store.state.kaelteschutz_art == "soll"


def test_im_notbetrieb_bleibt_der_notfall_boost_und_der_kaelteschutz_haelt_ihn_nach_dem_ende_des_notbetriebs(
    make_store, wall, uhr,
):
    rt = _neu(make_store, wall, uhr, {1: 18.0}, notbetrieb=True)
    _pruefen(rt)
    assert _lauft(rt) and _writes(rt) == EMERGENCY and rt.store.state.kaelteschutz_art is None

    _lauf(rt, 3 * STUNDE)

    assert rt.store.state.kaelteschutz_art == "soll" and _writes(rt) == EMERGENCY and _starts(rt) == 1

    rt.store.set_delivery(DeliveryState())  # der Server antwortet wieder: der Notbetrieb ist zu Ende
    _ist(rt, r1=19.0)
    _lauf(rt, 5 * MINUTE)

    assert _lauft(rt) and _writes(rt) == EMERGENCY

    _ist(rt, r1=20.5)
    _lauf(rt, 5 * MINUTE)

    assert not _lauft(rt) and _writes(rt) == EMERGENCY + RESTORE and rt.store.state.kaelteschutz_art is None


def test_ohne_kaelteschutz_endet_der_notfall_boost_des_notbetriebs_wie_bisher(make_store, wall, uhr):
    """Gegenprobe: ohne Lauf beendet das Ende des Notbetriebs den Notfall-Boost."""
    rt = _neu(make_store, wall, uhr, {1: 19.0}, notbetrieb=True)
    _pruefen(rt)
    assert _lauft(rt)

    rt.store.set_delivery(DeliveryState())
    _lauf(rt, 5 * MINUTE)

    assert not _lauft(rt) and _writes(rt) == EMERGENCY + RESTORE


# --- Kreise ------------------------------------------------------------------------------------------------------

def test_zwei_kreise_zaehlen_und_laufen_unabhaengig(tmp_path, wall, uhr):
    backup = {"last_room_target": 21.0, **RESTORE_POINT}
    kalt = _kreis(_store_in(tmp_path / "kreis1", backup), wall, uhr, {1: 18.0})
    warm = _kreis(_store_in(tmp_path / "kreis2", backup), wall, uhr, {1: 21.0})
    for kreis in (kalt, warm):
        _pruefen(kreis)
    for _ in range(36):
        wall.advance(5 * MINUTE)
        uhr.advance(5 * MINUTE)
        for kreis in (kalt, warm):
            _pruefen(kreis)

    assert _lauft(kalt) and kalt.store.state.kaelteschutz_art == "soll"
    assert not _lauft(warm) and _writes(warm) == [] and warm.store.state.kaelteschutz_art is None


def test_ein_vom_schreibbudget_aufgeschobenes_ende_haelt_den_lauf_bis_zum_naechsten_versuch(make_store, wall, uhr):
    rt = _neu(make_store, wall, uhr, {1: 4.0}, soll=None)
    _pruefen(rt)
    _lauf(rt, 15 * MINUTE, schritt=MINUTE)
    assert _lauft(rt)
    write_budget.after_failure(rt.store, write_budget.BOOST_END, write_budget.RETURN_STAIRCASE, rt.clock())

    _ist(rt, r1=9.0)
    _lauf(rt, MINUTE)

    assert _lauft(rt) and rt.store.state.kaelteschutz_art == "frost"  # das Ende ist nicht gebucht, der Lauf nicht verloren
    assert _writes(rt) == EMERGENCY

    _lauf(rt, 10 * MINUTE)

    assert not _lauft(rt) and rt.store.state.kaelteschutz_art is None
    assert _writes(rt) == EMERGENCY + RESTORE


def test_ein_aufgeschobenes_ende_haelt_auch_den_lauf_mit_soll_und_den_comfort_boost_unberuehrt(make_store, wall, uhr):
    rt = _gestartet(make_store, wall, uhr)
    write_budget.after_failure(rt.store, write_budget.BOOST_END, write_budget.RETURN_STAIRCASE, rt.clock())

    _ist(rt, r1=20.5)  # Ankunft
    _lauf(rt, MINUTE)

    assert _lauft(rt) and rt.store.state.kaelteschutz_art == "soll" and not rt.store.state.boost_active

    _lauf(rt, 10 * MINUTE)

    assert not _lauft(rt) and rt.store.state.kaelteschutz_art is None
    assert _writes(rt) == EMERGENCY + RESTORE
