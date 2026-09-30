"""Erkennung "Therme liefert keine Waerme" (TP12f, Spec 1): reine Logik, Messreihen als Fixtures."""
from datetime import datetime, timedelta, timezone

import pytest

from heizungsbruecke.waerme import (
    REQUEST_PAUSE_TOLERANCE,
    SHARE_THRESHOLD,
    WaermeState,
    evaluate,
    parse_since,
    share,
)

CEST = timezone(timedelta(hours=2))
TICK = timedelta(minutes=5)
T0 = datetime(2026, 9, 30, 2, 11, tzinfo=CEST)


def _at(hour, minute):
    return datetime(2026, 9, 30, hour, minute, tzinfo=CEST)


def _ticks(state, start, end, setpoint, flow, room=21.0):
    """Ein Tick alle 5 min von start (inklusive) bis end (exklusive) mit konstanten Werten."""
    ts = start
    while ts < end:
        state = evaluate(state, ts, setpoint, flow, room)
        ts += TICK
    return state


def _replay(rows, end, state=None):
    """rows: (Zeitpunkt, Vorlauf-Soll, Vorlauf, Raum); jede Zeile gilt bis zur naechsten. Tick alle 5 min
    von der ersten Zeile bis end (inklusive). Liefert [(Zeitpunkt, Zustand)]."""
    state = state or WaermeState()
    result, ts = [], rows[0][0]
    while ts <= end:
        _, setpoint, flow, room = [row for row in rows if row[0] <= ts][-1]
        state = evaluate(state, ts, setpoint, flow, room)
        result.append((ts, state))
        ts += TICK
    return result


# Nacht zum 30.09. (client1, Spec 1.3); 04:41 entspricht der 04:11-Zeile, weil die Tabelle sie auslaesst.
NIGHT = [
    (_at(2, 11), 35.68, 27.5, 21.6),
    (_at(2, 41), 36.46, 27.0, 21.3),
    (_at(3, 11), 36.79, 27.0, 21.3),
    (_at(3, 41), 37.55, 26.5, 21.3),
    (_at(4, 11), 37.88, 26.5, 21.0),
    (_at(5, 11), 37.88, 26.0, 21.0),
    (_at(5, 41), 0.0, 75.0, 21.0),       # Warmwasserladung
    (_at(6, 11), 38.94, 31.5, 20.7),     # Restwaerme: Anteil 0,59
    (_at(6, 41), 39.26, 29.0, 20.7),
    (_at(7, 41), 39.68, 27.0, 20.4),
]


def test_share_is_the_reached_part_of_the_requested_lift():
    assert share(40.0, 32.0, 20.0) == pytest.approx(0.6)
    assert share(40.0, 20.0, 20.0) == 0.0
    assert share(40.0, 40.0, 20.0) == 1.0


@pytest.mark.parametrize("args", [
    (24.0, 22.0, 21.0),                  # nur 3 K angefordert (unter MIN_LIFT)
    (None, 30.0, 21.0), (40.0, None, 21.0), (40.0, 30.0, None),
    (float("nan"), 30.0, 21.0), (40.0, float("inf"), 21.0), (True, 30.0, 21.0),
])
def test_share_is_none_without_a_usable_reading(args):
    assert share(*args) is None


def test_the_night_of_the_30th_sets_the_flag_at_the_three_hour_mark_and_keeps_it():
    states = dict(_replay(NIGHT, _at(7, 41)))
    assert states[_at(5, 6)].fehlt_seit is None
    assert states[_at(5, 11)].fehlt_seit == _at(5, 11)
    # Warmwasserladung und Restwaerme (Anteil 0,59 um 06:11) loeschen das Flag nicht.
    assert states[_at(6, 11)].fehlt_seit == _at(5, 11)
    assert states[_at(7, 41)].fehlt_seit == _at(5, 11)


def test_the_first_half_hour_of_a_phase_is_not_evaluated():
    state = _ticks(WaermeState(), T0, T0 + timedelta(minutes=25), 38.0, 35.0)
    assert state.unter_schwelle is False
    state = evaluate(state, T0 + timedelta(minutes=25), 38.0, 20.0, 21.0)
    assert state.unter_schwelle is False           # noch in SETTLE, trotz Anteil unter 0,5
    state = evaluate(state, T0 + timedelta(minutes=30), 38.0, 20.0, 21.0)
    assert state.unter_schwelle is True


def test_regular_burner_cycling_never_sets_the_flag():
    rows = [(T0 + timedelta(minutes=30 * i), 40.0, 36.0 if i % 2 == 0 else 30.0, 21.0) for i in range(24)]
    assert all(state.fehlt_seit is None for _, state in _replay(rows, T0 + timedelta(hours=11)))


def test_a_share_above_the_threshold_clears_the_flag_and_restarts_the_observation():
    rows = [
        (T0, 38.0, 26.0, 21.0),
        (T0 + timedelta(hours=4), 38.0, 33.0, 21.0),                  # Anteil 0,71: Waerme kommt an
        (T0 + timedelta(hours=4, minutes=5), 38.0, 26.0, 21.0),
    ]
    states = dict(_replay(rows, T0 + timedelta(hours=7)))
    assert states[T0 + timedelta(hours=3)].fehlt_seit == T0 + timedelta(hours=3)
    assert states[T0 + timedelta(hours=4)].fehlt_seit is None
    assert states[T0 + timedelta(hours=6, minutes=55)].fehlt_seit is None   # Beobachtung beginnt neu
    assert states[T0 + timedelta(hours=7)].fehlt_seit == T0 + timedelta(hours=7)


def test_a_share_exactly_at_the_threshold_clears_a_set_flag():
    # Vergleich ist >=: Anteil genau SHARE_THRESHOLD loescht, knapp darunter nicht (41 K Soll, 21 K Raum: Hub 20 K).
    assert share(41.0, 31.0, 21.0) == 0.5
    assert share(41.0, 31.0, 21.0) == SHARE_THRESHOLD
    flagged = WaermeState(fehlt_seit=_at(1, 0))
    rows_at = [(T0, 41.0, 21.0, 21.0), (T0 + timedelta(minutes=30), 41.0, 31.0, 21.0)]
    states = dict(_replay(rows_at, T0 + timedelta(minutes=30), state=flagged))
    assert states[T0 + timedelta(minutes=25)].fehlt_seit == _at(1, 0)     # SETTLE: noch nicht bewertet
    assert states[T0 + timedelta(minutes=30)].fehlt_seit is None
    rows_below = [(T0, 41.0, 21.0, 21.0), (T0 + timedelta(minutes=30), 41.0, 30.9, 21.0)]
    states = dict(_replay(rows_below, T0 + timedelta(minutes=30), state=flagged))
    assert states[T0 + timedelta(minutes=30)].fehlt_seit == _at(1, 0)


@pytest.mark.parametrize("extra, continues", [(timedelta(0), True), (timedelta(seconds=1), False)])
def test_a_request_pause_of_exactly_the_tolerance_continues_the_phase(extra, continues):
    # Vergleich ist strikt >: genau REQUEST_PAUSE_TOLERANCE seit dem letzten Anforderungs-Tick setzt die Phase fort,
    # eine Sekunde mehr beginnt eine neue (beobachtung_seit springt auf den Wiederbeginn).
    assert timedelta(minutes=45) == REQUEST_PAUSE_TOLERANCE
    state = evaluate(WaermeState(), T0, 40.0, 30.0, 21.0)
    state = evaluate(state, T0 + timedelta(minutes=10), 0.0, 75.0, 21.0)      # Unterbrechung (Warmwasser)
    resume = T0 + REQUEST_PAUSE_TOLERANCE + extra
    state = evaluate(state, resume, 40.0, 30.0, 21.0)
    assert state.beobachtung_seit == (T0 if continues else resume)
    assert state.settle_bis == resume + timedelta(minutes=30)      # beide Wege: 30 min Beruhigung


def test_a_set_flag_survives_a_new_phase_until_a_share_reaches_the_threshold():
    flagged = WaermeState(fehlt_seit=_at(1, 0))
    # 1 h ohne Anforderung, dann neue Phase mit gutem Vorlauf (Anteil 0,82).
    rows = [(_at(2, 0), 0.0, 20.0, 21.0), (_at(3, 0), 38.0, 35.0, 21.0)]
    states = dict(_replay(rows, _at(3, 30), state=flagged))
    assert states[_at(3, 25)].fehlt_seit == _at(1, 0)     # SETTLE nach Phasenbeginn: noch nicht bewertet
    assert states[_at(3, 30)].fehlt_seit is None


def test_requests_below_the_minimum_lift_change_nothing():
    state = _ticks(WaermeState(), T0, T0 + timedelta(hours=6), 24.0, 22.0)   # nur 3 K angefordert
    assert (state.fehlt_seit, state.unter_schwelle) == (None, False)


def test_missing_or_non_finite_readings_leave_the_state_alone():
    state = WaermeState()
    assert evaluate(state, T0, None, 30.0, 21.0) is state
    assert evaluate(state, T0, float("nan"), 30.0, 21.0) is state
    started = evaluate(state, T0, 38.0, None, 21.0)                            # Phase beginnt, keine Bewertung
    assert started.beobachtung_seit == T0
    assert _ticks(started, T0 + TICK, T0 + timedelta(hours=6), 38.0, None).fehlt_seit is None
    assert _ticks(started, T0 + TICK, T0 + timedelta(hours=6), 38.0, float("nan")).fehlt_seit is None


def test_a_short_pause_continues_the_phase_and_a_long_one_starts_a_new_one():
    running = _ticks(WaermeState(), T0, T0 + timedelta(hours=2), 38.0, 26.0)
    assert running.beobachtung_seit == T0 and running.unter_schwelle is True

    def resume(pause_minutes):
        last_request = T0 + timedelta(hours=2) - TICK
        resume_at = last_request + timedelta(minutes=pause_minutes)
        paused = _ticks(running, T0 + timedelta(hours=2), resume_at, 0.0, 75.0)
        return resume_at, evaluate(paused, resume_at, 38.0, 26.0, 21.0)

    _, short = resume(40)
    assert short.beobachtung_seit == T0 and short.unter_schwelle is True
    resume_at, long = resume(50)
    assert long.beobachtung_seit == resume_at and long.unter_schwelle is False


def test_parse_since_accepts_only_iso_text_with_a_time_zone():
    assert parse_since("2026-09-30T05:11:00+02:00") == _at(5, 11)
    for bad in (None, 5, "", "gestern", "2026-09-30T05:11:00"):
        assert parse_since(bad) is None
