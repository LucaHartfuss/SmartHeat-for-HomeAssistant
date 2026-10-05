"""Wanduhr des Clients (Spec SHG 3.2, Plan-Praezisierung 1)."""
import re
from datetime import datetime
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
