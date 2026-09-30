"""Schreibbudget fuer Cloud-Schreibvorgaenge (TP12b, Spec 3.1)."""
import pytest

from heizungsbruecke import write_budget as wb

DAY = "2026-10-01"


@pytest.mark.parametrize("rule", [wb.QUOTA, wb.RETRY_QUOTA, wb.RETURN_STAIRCASE])
def test_first_attempt_is_always_allowed(rule):
    assert wb.allowed(None, rule, 0.0, DAY) is True


@pytest.mark.parametrize("count,wait", [(1, 300), (2, 900), (3, 1800), (7, 1800)])
def test_return_staircase_waits(count, wait):
    entry = {"day": DAY, "count": count, "last": 1000.0}
    assert wb.allowed(entry, wb.RETURN_STAIRCASE, 1000.0 + wait - 1, DAY) is False
    assert wb.allowed(entry, wb.RETURN_STAIRCASE, 1000.0 + wait, DAY) is True


def test_return_staircase_has_no_daily_limit():
    entry = {"day": DAY, "count": 100, "last": 0.0}
    assert wb.allowed(entry, wb.RETURN_STAIRCASE, 1800.0, DAY) is True


@pytest.mark.parametrize("rule", [wb.QUOTA, wb.RETRY_QUOTA])
def test_quota_rules_wait_30_minutes_and_stop_at_six_a_day(rule):
    assert wb.allowed({"day": DAY, "count": 1, "last": 0.0}, rule, 1799.0, DAY) is False
    assert wb.allowed({"day": DAY, "count": 1, "last": 0.0}, rule, 1800.0, DAY) is True
    assert wb.allowed({"day": DAY, "count": 6, "last": 0.0}, rule, 99999.0, DAY) is False


def test_new_day_starts_fresh():
    entry = {"day": "2026-09-30", "count": 6, "last": 5000.0}
    assert wb.allowed(entry, wb.QUOTA, 5001.0, DAY) is True
    assert wb.counted(entry, 5001.0, DAY) == {"day": DAY, "count": 1, "last": 5001.0}


def test_loaded_entry_without_last_keeps_the_daily_limit():
    assert wb.allowed({"day": DAY, "count": 6}, wb.QUOTA, 0.0, DAY) is False
    assert wb.allowed({"day": DAY, "count": 2}, wb.QUOTA, 0.0, DAY) is True


def test_restart_keeps_the_staircase_position_but_not_the_wait():
    loaded = {"day": DAY, "count": 1}  # ein Fehlschlag vor dem Neustart, "last" verworfen
    assert wb.allowed(loaded, wb.RETURN_STAIRCASE, 10.0, DAY) is True  # sofort nach dem Start
    after = wb.counted(loaded, 10.0, DAY)  # scheitert erneut
    assert wb.allowed(after, wb.RETURN_STAIRCASE, 10.0 + 899, DAY) is False
    assert wb.allowed(after, wb.RETURN_STAIRCASE, 10.0 + 900, DAY) is True


def test_limit_first_reached_only_once_a_day():
    entry = {"day": DAY, "count": 6, "last": 0.0}
    assert wb.limit_first_reached(entry, wb.QUOTA, DAY) is True
    assert wb.limit_first_reached({**entry, "limit_notified": DAY}, wb.QUOTA, DAY) is False
    assert wb.limit_first_reached(entry, wb.RETURN_STAIRCASE, DAY) is False


def test_put_is_best_effort_and_keeps_memory(make_store, monkeypatch):
    store = make_store()

    def _broken(*args, **kwargs):
        raise OSError("Datentraeger kaputt")

    monkeypatch.setattr("heizungsbruecke.backup_store.save_backup", _broken)

    wb.put(store, wb.BOOST_END, {"day": DAY, "count": 1, "last": 1.0})

    assert wb.get(store, wb.BOOST_END) == {"day": DAY, "count": 1, "last": 1.0}
    assert store.storage_failed is True


def test_record_success_removes_only_an_existing_key(make_store, tmp_path, monkeypatch):
    store = make_store()
    saves = []
    monkeypatch.setattr("heizungsbruecke.backup_store.save_backup", lambda path, values: saves.append(values))

    wb.record_success(store, wb.RESTORE)  # nichts da -> kein Schreiben
    assert saves == []

    wb.record_attempt(store, wb.RESTORE, 5.0)
    wb.record_success(store, wb.RESTORE)
    assert wb.get(store, wb.RESTORE) is None
