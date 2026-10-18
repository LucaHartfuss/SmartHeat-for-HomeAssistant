import multiprocessing
import os
from unittest import mock

import pytest

import smartheat_gateway.quota as quota_module
from smartheat_gateway.quota import QuotaExhausted, QuotaGuard, QuotaSpec

SPEC = QuotaSpec(hard_limit=3, window_seconds=100)


def make_guard(tmp_path):
    return QuotaGuard(tmp_path / "quota" / "q.json", SPEC, now=lambda: 1000.0)


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
    guard = QuotaGuard(path, QuotaSpec(hard_limit=1000, window_seconds=3600))
    for _ in range(count):
        guard.take()


def test_two_processes_share_one_counter(tmp_path):
    path = tmp_path / "q.json"
    workers = [multiprocessing.Process(target=_take_many, args=(path, 50)) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(30)
    assert [worker.exitcode for worker in workers] == [0, 0]
    assert QuotaGuard(path, SPEC).used() == 100


def test_save_goes_through_the_atomic_writer(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(quota_module.dateien, "schreiben_json", lambda path, data, **kw: calls.append((path, data)))
    guard = make_guard(tmp_path)
    guard.take()
    assert calls and calls[-1][0] == guard._path


def test_corrupt_or_foreign_file_counts_as_empty(tmp_path):
    guard = make_guard(tmp_path)
    guard._path.parent.mkdir(parents=True, exist_ok=True)
    for content in ("{kaputt", "[1, 2]", ""):
        guard._path.write_text(content)
        assert guard.used() == 0


def test_a_forward_clock_jump_at_boot_lets_old_calls_expire_early_but_never_blocks(tmp_path):
    now = [1_000_000.0]
    guard = QuotaGuard(tmp_path / "q.json", QuotaSpec(5, 86400), now=lambda: now[0])
    for _ in range(5):
        guard.take()
    assert guard.exhausted()
    now[0] += 90_000  # Sprung nach vorn (Pi ohne Echtzeituhr, NTP stellt die Uhr)
    assert not guard.exhausted() and guard.used() == 0


def test_a_backward_clock_jump_drops_calls_from_the_future_at_once(tmp_path):
    now = [1_000_000.0]
    guard = QuotaGuard(tmp_path / "q.json", QuotaSpec(5, 86400), now=lambda: now[0])
    for _ in range(5):
        guard.take()
    now[0] -= 3 * 86400  # Sprung zurueck: die Eintraege liegen jetzt in der Zukunft
    assert guard.used() == 0  # quota._load behaelt nur now - window < ts <= now + 1 (Praezisierung 8)


@pytest.mark.parametrize("abbruch", ["fsync", "replace"])
def test_the_counter_survives_a_crash_while_saving(tmp_path, monkeypatch, abbruch):
    """Atomar mit fsync (Plan G2b-1, Restpunkt 8): bricht das Schreiben ab (Stromausfall, volle Platte), bleibt die alte Datei
    unveraendert lesbar, der Zaehler also bei zwei Aufrufen; der Abbruch erreicht den Aufrufer, danach zaehlt der naechste
    Aufruf normal weiter (die Sperre ist freigegeben, ein Rest der Zwischendatei stoert nicht). Bricht schon der fsync der
    Zwischendatei ab, ist sie noch nicht an ihren Platz getreten: erst fsync, dann replace."""
    guard = make_guard(tmp_path)
    guard.take()
    guard.take()
    vorher = guard._path.read_bytes()
    # Die Attrappen an os.fsync/os.replace (autospec) sind die Dateisystem-Grenze (Nr. 23): sie spielen den Ausfall des
    # Systems nach; geprueft wird die Wirkung auf die Datei, nicht der Aufruf.
    assert guard.used() == 2
    with monkeypatch.context() as patch:
        patch.setattr(os, abbruch, mock.create_autospec(getattr(os, abbruch), side_effect=OSError(5, "Abbruch")))
        with pytest.raises(OSError, match="Abbruch"):
            guard.take()
    assert guard._path.read_bytes() == vorher
    assert make_guard(tmp_path).used() == 2  # frische Instanz liest die alte Datei
    guard.take()  # Gegenprobe: ohne Abbruch zaehlt es wieder, die Sperre haengt nicht
    assert make_guard(tmp_path).used() == 3
