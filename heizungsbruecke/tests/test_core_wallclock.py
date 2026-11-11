"""Wanduhr des Clients."""
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from smartheat_core import wallclock

SRC = Path(__file__).resolve().parents[1] / "src"
BERLIN = ZoneInfo("Europe/Berlin")


def test_now_is_an_aware_local_time():
    assert wallclock.now().tzinfo is not None


def test_now_and_today_follow_the_set_clock(monkeypatch):
    fixed = datetime(2026, 3, 29, 3, 30, tzinfo=BERLIN)  # kurz nach der Zeitumstellung
    monkeypatch.setattr(wallclock, "_now", lambda: fixed)
    assert wallclock.now() == fixed
    assert wallclock.today().isoformat() == "2026-03-29"


def test_today_is_the_local_date_not_the_utc_date(monkeypatch):
    # 00:30 in Berlin ist in UTC noch der Vortag 23:30; Tagestick und Tagesbudget zaehlen nach Ortszeit.
    monkeypatch.setattr(wallclock, "_now", lambda: datetime(2026, 1, 15, 0, 30, tzinfo=BERLIN))
    assert wallclock.today().isoformat() == "2026-01-15"


def test_only_the_wallclock_reads_the_system_time():
    pattern = re.compile(r"\bdatetime\.now\(|\bdate\.today\(")
    offenders = [
        f"{path.relative_to(SRC)}:{number}"
        for path in sorted(SRC.rglob("*.py"))
        if path.name != "wallclock.py"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if pattern.search(line)
    ]
    assert offenders == []


# --- Vertrauen in die Uhr -------------------------------------------------------------------------------------------

T0 = datetime(2026, 11, 11, 12, 0, tzinfo=BERLIN)


def _wand(sekunden: float) -> datetime:
    return T0 + timedelta(seconds=sekunden)


def test_sprungtoleranz_ist_fuenf_minuten():
    # Gepinnt: Planer-Annahme, kuerzer ruht die Regelung oefter, laenger laesst einen Sprung zu.
    assert wallclock.UHR_SPRUNG_TOLERANZ_S == 300.0


def test_ohne_synchrone_meldung_seit_dem_start_kein_vertrauen():
    # Neustart ohne Zeitdienst: nie synchron gemeldet, die Wanduhr laeuft gleich wie die monotone, trotzdem kein Vertrauen.
    beleg = wallclock.beleg_fortschreiben(None, synchron=False, wand_jetzt=_wand(0), mono_jetzt=1000.0)
    assert beleg is None
    assert not wallclock.vertrauenswuerdig(beleg, wand_jetzt=_wand(60), mono_jetzt=1060.0)


def test_synchron_gemeldet_und_gleichlaufend_ist_vertrauenswuerdig():
    beleg = wallclock.beleg_fortschreiben(None, synchron=True, wand_jetzt=_wand(0), mono_jetzt=1000.0)
    assert wallclock.vertrauenswuerdig(beleg, wand_jetzt=_wand(0), mono_jetzt=1000.0)
    assert wallclock.vertrauenswuerdig(beleg, wand_jetzt=_wand(3600), mono_jetzt=4600.0)


def test_sprung_vor_ueber_der_toleranz_nimmt_das_vertrauen():
    beleg = wallclock.beleg_fortschreiben(None, synchron=True, wand_jetzt=_wand(0), mono_jetzt=1000.0)
    assert not wallclock.vertrauenswuerdig(beleg, wand_jetzt=_wand(60 + 301), mono_jetzt=1060.0)


def test_sprung_zurueck_ueber_der_toleranz_nimmt_das_vertrauen():
    beleg = wallclock.beleg_fortschreiben(None, synchron=True, wand_jetzt=_wand(0), mono_jetzt=1000.0)
    assert not wallclock.vertrauenswuerdig(beleg, wand_jetzt=_wand(3600 - 301), mono_jetzt=4600.0)


def test_sprung_genau_an_der_toleranz_zaehlt_noch_als_vertrauenswuerdig():
    beleg = wallclock.beleg_fortschreiben(None, synchron=True, wand_jetzt=_wand(0), mono_jetzt=1000.0)
    assert wallclock.vertrauenswuerdig(beleg, wand_jetzt=_wand(60 + 300), mono_jetzt=1060.0)
    assert wallclock.vertrauenswuerdig(beleg, wand_jetzt=_wand(60 - 300), mono_jetzt=1060.0)
    assert not wallclock.vertrauenswuerdig(beleg, wand_jetzt=_wand(60 + 300.5), mono_jetzt=1060.0)


def test_verlust_der_synchronitaet_allein_nimmt_das_vertrauen_nicht():
    # Einmal synchron seit dem Start genuegt, solange die Wanduhr gegen die monotone Uhr nicht springt.
    beleg = wallclock.beleg_fortschreiben(None, synchron=True, wand_jetzt=_wand(0), mono_jetzt=1000.0)
    beleg = wallclock.beleg_fortschreiben(beleg, synchron=False, wand_jetzt=_wand(30), mono_jetzt=1030.0)
    assert wallclock.vertrauenswuerdig(beleg, wand_jetzt=_wand(90), mono_jetzt=1090.0)


def test_ohne_synchronitaet_bleibt_der_sprung_sichtbar_und_die_rueckkehr_gibt_das_vertrauen_zurueck():
    beleg = wallclock.beleg_fortschreiben(None, synchron=True, wand_jetzt=_wand(0), mono_jetzt=1000.0)
    # Sprung vor um eine Stunde, der Zeitdienst meldet nicht synchron: der Beleg bleibt alt.
    beleg = wallclock.beleg_fortschreiben(beleg, synchron=False, wand_jetzt=_wand(30 + 3600), mono_jetzt=1030.0)
    assert not wallclock.vertrauenswuerdig(beleg, wand_jetzt=_wand(60 + 3600), mono_jetzt=1060.0)
    # Der Zeitdienst meldet wieder synchron: neuer Beleg, die Uhr gilt wieder.
    beleg = wallclock.beleg_fortschreiben(beleg, synchron=True, wand_jetzt=_wand(90 + 3600), mono_jetzt=1090.0)
    assert wallclock.vertrauenswuerdig(beleg, wand_jetzt=_wand(120 + 3600), mono_jetzt=1120.0)


def test_beleg_bleibt_ohne_synchrone_meldung_unveraendert():
    beleg = wallclock.beleg_fortschreiben(None, synchron=True, wand_jetzt=_wand(0), mono_jetzt=1000.0)
    assert wallclock.beleg_fortschreiben(beleg, synchron=False, wand_jetzt=_wand(500), mono_jetzt=1500.0) is beleg


def test_vertrauen_hat_keine_wanduhr_und_keine_monotone_uhr_im_zugriff(monkeypatch):
    # Reine Funktion: die Zeiten kommen als Argumente, das Ergebnis haengt nicht an der gesetzten Wanduhr.
    monkeypatch.setattr(wallclock, "_now", lambda: datetime(1999, 1, 1, tzinfo=BERLIN))
    beleg = wallclock.beleg_fortschreiben(None, synchron=True, wand_jetzt=_wand(0), mono_jetzt=1000.0)
    assert wallclock.vertrauenswuerdig(beleg, wand_jetzt=_wand(10), mono_jetzt=1010.0)
