import json
import os
import threading
import time
from unittest import mock

import pytest

import smartheat_gateway.quota as quota_module
from smartheat_gateway.quota import QuotaExhausted, QuotaGuard, QuotaSpec

SPEC = QuotaSpec(hard_limit=3, window_seconds=100)


def make_guard(tmp_path):
    return QuotaGuard(tmp_path / "quota" / "q.json", SPEC, now=lambda: 1000.0)


def zaehler(guard):
    """Zahl der Aufrufe, die die Datei fuehrt (der Waechter selbst bietet keine Zaehlung)."""
    return len(json.loads(guard._path.read_text())["aufrufe"])


def test_window_and_hard_limit(tmp_path):
    now = [1000.0]
    guard = QuotaGuard(tmp_path / "q.json", SPEC, now=lambda: now[0])
    for _ in range(3):
        guard.take()
    assert guard.exhausted()
    with pytest.raises(QuotaExhausted):
        guard.take()
    now[0] += 101
    guard.take()
    assert zaehler(guard) == 1  # die vier alten Eintraege sind mit dem Fenster verfallen


def test_rate_limit_blocks_until_the_window_is_free(tmp_path):
    now = [1000.0]
    guard = QuotaGuard(tmp_path / "q.json", SPEC, now=lambda: now[0])
    guard.block_until(1050.0)
    assert guard.exhausted()
    now[0] = 1051.0
    assert not guard.exhausted()


def test_two_guards_in_two_threads_share_one_counter(tmp_path, monkeypatch):
    """Befehle des Geraetekerns und Laufzeit bauen je einen Waechter auf derselben Datei (Spec SHG G2 4.2); in einem
    Prozess schuetzt sie ein Lock je Pfad. Das Laden mit einer Pause von 1 ms macht den Wettlauf des Lesens und
    Schreibens sicher sichtbar."""
    laden = QuotaGuard._load

    def langsam(self):
        state = laden(self)
        time.sleep(0.001)
        return state

    monkeypatch.setattr(QuotaGuard, "_load", langsam)
    path = tmp_path / "q.json"
    spec = QuotaSpec(hard_limit=1000, window_seconds=3600)

    def nehmen():
        guard = QuotaGuard(path, spec)
        for _ in range(50):
            guard.take()

    threads = [threading.Thread(target=nehmen) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not any(thread.is_alive() for thread in threads)
    assert zaehler(QuotaGuard(path, spec)) == 100


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
        assert not guard.exhausted()
        guard.take()
        assert zaehler(guard) == 1  # die Datei ist wieder heil und fuehrt nur den neuen Aufruf


def test_a_forward_clock_jump_at_boot_lets_old_calls_expire_early_but_never_blocks(tmp_path):
    now = [1_000_000.0]
    guard = QuotaGuard(tmp_path / "q.json", QuotaSpec(5, 86400), now=lambda: now[0])
    for _ in range(5):
        guard.take()
    assert guard.exhausted()
    now[0] += 90_000  # Sprung nach vorn (Pi ohne Echtzeituhr, NTP stellt die Uhr)
    assert not guard.exhausted()
    guard.take()
    assert zaehler(guard) == 1  # die fuenf alten Eintraege sind verfallen


def test_a_backward_clock_jump_keeps_counting_until_the_window_ends(tmp_path):
    """Eintraege aus der Zukunft (Uhr springt zurueck) zaehlen ab dem Sprung weiter: auf `now` begrenzt, laufen sie
    mit dem Fenster ab. Ein Verwerfen liesse das Kontingent nach jedem Uhrsprung frei."""
    now = [1_000_000.0]
    guard = QuotaGuard(tmp_path / "q.json", QuotaSpec(5, 86400), now=lambda: now[0])
    for _ in range(5):
        guard.take()
    now[0] -= 3 * 86400  # Sprung zurueck: die Eintraege liegen jetzt in der Zukunft
    assert guard.exhausted()
    with pytest.raises(QuotaExhausted):
        guard.take()
    now[0] += 86399  # fast ein Fenster nach dem Sprung: noch gezaehlt
    assert guard.exhausted()
    now[0] += 2  # Fenster plus eine Sekunde nach dem Sprung: frei
    assert not guard.exhausted()
    guard.take()
    assert zaehler(guard) == 1


def _datei_schreiben(guard, inhalt):
    guard._path.parent.mkdir(parents=True, exist_ok=True)
    guard._path.write_text(json.dumps(inhalt))


@pytest.mark.parametrize("zukunft", [10**12, 10**18, float("inf")])
def test_a_huge_future_entry_blocks_at_most_one_window(tmp_path, zukunft):
    """Ein kaputter oder riesiger Zeitstempel in der Zaehlerliste zaehlt wie ein Eintrag von `now`: hoechstens ein
    Fenster lang, nie dauerhaft."""
    now = [1_000_000.0]
    guard = QuotaGuard(tmp_path / "q.json", QuotaSpec(5, 86400), now=lambda: now[0])
    _datei_schreiben(guard, {"aufrufe": [zukunft] * 5, "gesperrt_bis": None})
    assert guard.exhausted()
    assert json.loads(guard._path.read_text())["aufrufe"] == [now[0]] * 5  # der gekappte Wert ist gespeichert
    now[0] += 86401
    assert not guard.exhausted()


@pytest.mark.parametrize("gesperrt_bis", [10**12, 10**18, float("inf")])
def test_a_huge_block_time_blocks_at_most_one_window(tmp_path, gesperrt_bis):
    """`gesperrt_bis` (429) ist auf `now + Fenster` begrenzt: ein Uhrsprung zurueck oder ein kaputter Wert sperrt nie
    laenger als ein Fenster, auch nicht bis die Datei geloescht wird."""
    now = [1_000_000.0]
    guard = QuotaGuard(tmp_path / "q.json", SPEC, now=lambda: now[0])
    _datei_schreiben(guard, {"aufrufe": [], "gesperrt_bis": gesperrt_bis})
    assert guard.exhausted()
    assert json.loads(guard._path.read_text())["gesperrt_bis"] == now[0] + SPEC.window_seconds
    now[0] += SPEC.window_seconds - 1
    assert guard.exhausted()
    now[0] += 2
    assert not guard.exhausted()


def test_a_block_time_that_is_not_a_number_is_ignored(tmp_path):
    guard = make_guard(tmp_path)
    _datei_schreiben(guard, {"aufrufe": [], "gesperrt_bis": float("nan")})
    assert not guard.exhausted()


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
    assert zaehler(guard) == 2
    with monkeypatch.context() as patch:
        patch.setattr(os, abbruch, mock.create_autospec(getattr(os, abbruch), side_effect=OSError(5, "Abbruch")))
        with pytest.raises(OSError, match="Abbruch"):
            guard.take()
    assert guard._path.read_bytes() == vorher
    assert zaehler(make_guard(tmp_path)) == 2  # frische Instanz liest die alte Datei
    guard.take()  # Gegenprobe: ohne Abbruch zaehlt es wieder, die Sperre haengt nicht
    assert zaehler(make_guard(tmp_path)) == 3
