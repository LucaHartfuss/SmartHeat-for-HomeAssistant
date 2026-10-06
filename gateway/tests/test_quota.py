import multiprocessing

import pytest

from smartheat_gateway.quota import QuotaExhausted, QuotaGuard, QuotaSpec

SPEC = QuotaSpec(limit=5, hard_limit=3, window_seconds=100)


def test_window_and_hard_limit(tmp_path):
    now = [1000.0]
    guard = QuotaGuard(tmp_path / "q.json", SPEC, now=lambda: now[0])
    for _ in range(3):
        guard.take()
    assert guard.exhausted()
    with pytest.raises(QuotaExhausted):
        guard.take()
    now[0] += 101
    assert guard.used() == 0
    guard.take()


def test_rate_limit_blocks_until_the_window_is_free(tmp_path):
    now = [1000.0]
    guard = QuotaGuard(tmp_path / "q.json", SPEC, now=lambda: now[0])
    guard.block_until(1050.0)
    assert guard.exhausted()
    now[0] = 1051.0
    assert not guard.exhausted()


def _take_many(path, count):
    guard = QuotaGuard(path, QuotaSpec(limit=1000, hard_limit=1000, window_seconds=3600))
    for _ in range(count):
        guard.take()


def test_two_processes_share_one_counter(tmp_path):
    path = tmp_path / "q.json"
    workers = [multiprocessing.Process(target=_take_many, args=(path, 50)) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(30)
    assert QuotaGuard(path, SPEC).used() == 100
