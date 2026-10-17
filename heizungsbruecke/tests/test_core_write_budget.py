"""Schreibbudget fuer Cloud-Schreibvorgaenge (TP12b, Spec 3.1)."""
import pytest

from smartheat_core import write_budget as wb

# Durchsetzen mit dem Standard-Tageslimit (frueher write_budget.QUOTA).
QUOTA = wb.enforce_rule(wb.MAX_PER_DAY)
DAY = "2026-10-01"


@pytest.mark.parametrize("rule", [QUOTA, wb.RETRY_QUOTA, wb.RETURN_STAIRCASE])
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


@pytest.mark.parametrize("rule", [QUOTA, wb.RETRY_QUOTA])
def test_quota_rules_wait_30_minutes_and_stop_at_six_a_day(rule):
    assert wb.allowed({"day": DAY, "count": 1, "last": 0.0}, rule, 1799.0, DAY) is False
    assert wb.allowed({"day": DAY, "count": 1, "last": 0.0}, rule, 1800.0, DAY) is True
    assert wb.allowed({"day": DAY, "count": 6, "last": 0.0}, rule, 99999.0, DAY) is False


def test_new_day_starts_fresh():
    entry = {"day": "2026-09-30", "count": 6, "last": 5000.0}
    assert wb.allowed(entry, QUOTA, 5001.0, DAY) is True
    assert wb.counted(entry, 5001.0, DAY) == {"day": DAY, "count": 1, "last": 5001.0}


def test_loaded_entry_without_last_keeps_the_daily_limit():
    assert wb.allowed({"day": DAY, "count": 6}, QUOTA, 0.0, DAY) is False
    assert wb.allowed({"day": DAY, "count": 2}, QUOTA, 0.0, DAY) is True


def test_restart_keeps_the_staircase_position_but_not_the_wait():
    loaded = {"day": DAY, "count": 1}  # ein Fehlschlag vor dem Neustart, "last" verworfen
    assert wb.allowed(loaded, wb.RETURN_STAIRCASE, 10.0, DAY) is True  # sofort nach dem Start
    after = wb.counted(loaded, 10.0, DAY)  # scheitert erneut
    assert wb.allowed(after, wb.RETURN_STAIRCASE, 10.0 + 899, DAY) is False
    assert wb.allowed(after, wb.RETURN_STAIRCASE, 10.0 + 900, DAY) is True


def test_limit_first_reached_only_once_a_day():
    entry = {"day": DAY, "count": 6, "last": 0.0}
    assert wb.limit_first_reached(entry, QUOTA, DAY) is True
    assert wb.limit_first_reached({**entry, "limit_notified": DAY}, QUOTA, DAY) is False
    assert wb.limit_first_reached(entry, wb.RETURN_STAIRCASE, DAY) is False


def test_put_is_best_effort_and_keeps_memory(make_store, monkeypatch):
    store = make_store()

    def _broken(*args, **kwargs):
        raise OSError("Datentraeger kaputt")

    monkeypatch.setattr("smartheat_runtime.backup_store.save_backup", _broken)

    wb.put(store, wb.BOOST_END, {"day": DAY, "count": 1, "last": 1.0})

    assert wb.get(store, wb.BOOST_END) == {"day": DAY, "count": 1, "last": 1.0}
    assert store.storage_failed is True


def test_after_success_removes_only_an_existing_key(make_store, tmp_path, monkeypatch):
    store = make_store()
    saves = []
    monkeypatch.setattr("smartheat_runtime.backup_store.save_backup", lambda path, values: saves.append(values))

    wb.after_success(store, wb.RESTORE, wb.RETRY_QUOTA)  # nichts da -> kein Schreiben
    assert saves == []

    wb.after_failure(store, wb.RESTORE, wb.RETRY_QUOTA, 5.0)
    wb.after_success(store, wb.RESTORE, wb.RETRY_QUOTA)
    assert wb.get(store, wb.RESTORE) is None


@pytest.mark.parametrize(("rule", "erfolg", "zaehlung"), [
    (QUOTA, True, 1), (QUOTA, False, 1),
    (wb.enforce_rule(4), True, 1), (wb.enforce_rule(4), False, 1),
    (wb.RETRY_QUOTA, True, 0), (wb.RETRY_QUOTA, False, 1),
    (wb.RETURN_STAIRCASE, True, 0), (wb.RETURN_STAIRCASE, False, 1),
])
def test_the_rule_decides_what_counts(make_store, rule, erfolg, zaehlung):
    store = make_store()
    wb.before_attempt(store, "k", rule, 10.0)
    if erfolg:
        wb.after_success(store, "k", rule)
    else:
        wb.after_failure(store, "k", rule, 10.0)
    assert wb.count_today(wb.get(store, "k"), wb.today()) == zaehlung


def test_a_success_resets_only_rules_that_count_failures(make_store):
    store = make_store()
    wb.after_failure(store, "k", wb.RETRY_QUOTA, 1.0)
    wb.after_success(store, "k", wb.RETRY_QUOTA)
    assert wb.get(store, "k") is None
    wb.before_attempt(store, "e", QUOTA, 1.0)
    wb.after_success(store, "e", QUOTA)
    assert wb.count_today(wb.get(store, "e"), wb.today()) == 1


def test_an_explicit_day_is_used_for_counting(make_store):
    store = make_store()
    wb.before_attempt(store, "k", QUOTA, 1.0, day=DAY)
    wb.after_failure(store, "f", wb.RETRY_QUOTA, 1.0, day=DAY)
    assert wb.get(store, "k") == {"day": DAY, "count": 1, "last": 1.0}
    assert wb.get(store, "f") == {"day": DAY, "count": 1, "last": 1.0}


def test_enforce_rule_with_the_standard_limit_is_every_attempt_one_per_interval_six_per_day():
    assert (QUOTA.every_attempt, QUOTA.waits, QUOTA.max_per_day) == (True, (wb.INTERVAL_SECONDS,), 6)
