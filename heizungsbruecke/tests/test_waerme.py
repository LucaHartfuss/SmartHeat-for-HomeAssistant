"""Erkennung "Therme liefert keine Waerme" (TP12f, Spec 1): reine Logik, Messreihen als Fixtures."""
import random
from datetime import datetime, timedelta, timezone

import pytest

from heizungsbruecke.waerme import (
    CLEAR_WINDOW,
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


def test_a_window_with_a_median_share_above_the_threshold_clears_the_flag_and_restarts_the_observation():
    assert timedelta(minutes=60) == CLEAR_WINDOW
    rows = [
        (T0, 38.0, 26.0, 21.0),
        (T0 + timedelta(hours=4), 38.0, 33.0, 21.0),                  # Anteil 0,71: Waerme kommt an
        (T0 + timedelta(hours=5, minutes=5), 38.0, 26.0, 21.0),
    ]
    states = dict(_replay(rows, T0 + timedelta(hours=8)))
    assert states[T0 + timedelta(hours=3)].fehlt_seit == T0 + timedelta(hours=3)
    # Fenster 3:25-4:25: 7 Ticks darunter, 6 darueber -> Median darunter; 3:30-4:30: 6 darunter, 7 darueber.
    assert states[T0 + timedelta(hours=4, minutes=25)].fehlt_seit == T0 + timedelta(hours=3)
    assert states[T0 + timedelta(hours=4, minutes=30)].fehlt_seit is None
    assert states[T0 + timedelta(hours=7, minutes=55)].fehlt_seit is None    # Beobachtung beginnt neu (zuletzt 5:00)
    assert states[T0 + timedelta(hours=8)].fehlt_seit == T0 + timedelta(hours=8)


def test_a_share_exactly_at_the_threshold_clears_a_set_flag_once_the_window_is_covered():
    # Vergleich ist >=: Anteil genau SHARE_THRESHOLD loescht, knapp darunter nicht (41 K Soll, 21 K Raum: Hub 20 K).
    assert share(41.0, 31.0, 21.0) == 0.5
    assert share(41.0, 31.0, 21.0) == SHARE_THRESHOLD
    flagged = WaermeState(fehlt_seit=_at(1, 0))
    evaluated = T0 + timedelta(minutes=30)                                    # erster bewerteter Tick (nach SETTLE)
    end = evaluated + CLEAR_WINDOW
    rows_at = [(T0, 41.0, 21.0, 21.0), (evaluated, 41.0, 31.0, 21.0)]
    states = dict(_replay(rows_at, end, state=flagged))
    assert states[T0 + timedelta(minutes=25)].fehlt_seit == _at(1, 0)         # SETTLE: noch nicht bewertet
    assert len(states[evaluated].anteile) == 1
    assert states[end - TICK].fehlt_seit == _at(1, 0)                         # aeltester Wert erst 55 min alt
    assert states[end].fehlt_seit is None                                     # genau CLEAR_WINDOW: Fenster voll
    rows_below = [(T0, 41.0, 21.0, 21.0), (evaluated, 41.0, 30.9, 21.0)]
    states = dict(_replay(rows_below, end + timedelta(hours=1), state=flagged))
    assert all(state.fehlt_seit == _at(1, 0) for ts, state in states.items() if ts >= evaluated)


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


def test_a_set_flag_survives_a_new_phase_until_the_window_median_reaches_the_threshold():
    flagged = WaermeState(fehlt_seit=_at(1, 0))
    # 1 h ohne Anforderung, dann neue Phase mit gutem Vorlauf (Anteil 0,82).
    rows = [(_at(2, 0), 0.0, 20.0, 21.0), (_at(3, 0), 38.0, 35.0, 21.0)]
    states = dict(_replay(rows, _at(4, 30), state=flagged))
    assert states[_at(3, 25)].fehlt_seit == _at(1, 0)     # SETTLE nach Phasenbeginn: noch nicht bewertet
    assert states[_at(3, 30)].fehlt_seit == _at(1, 0)     # erster bewerteter Tick: das Fenster beginnt sich zu fuellen
    assert states[_at(4, 25)].fehlt_seit == _at(1, 0)
    assert states[_at(4, 30)].fehlt_seit is None


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


F = _at(1, 0)                                          # gesetztes Flag der Fenster-Tests
E = T0 + timedelta(minutes=30)                         # erster bewerteter Tick nach SETTLE


def _flagged_after_settle():
    """Gesetztes Flag, neue Phase ab T0, SETTLE (30 min) vorbei; der naechste Tick (E) ist der erste bewertete."""
    return _ticks(WaermeState(fehlt_seit=F), T0, E, 41.0, 21.0)


def _trace(state, start, shares, per=1, soll=41.0, room=21.0):
    """Je Anteil `per` Ticks (5 min) ab start (per=6: eine ~30-min-Abfrage); liefert [(Zeitpunkt, Zustand)]."""
    out, ts = [], start
    for value in shares:
        for _ in range(per):
            state = evaluate(state, ts, soll, room + value * (soll - room), room)
            out.append((ts, state))
            ts += TICK
    return out


def _cleared_at(states):
    return next((ts for ts, state in states if state.fehlt_seit is None), None)


def test_a_single_poll_with_residual_heat_does_not_clear_the_flag():
    # Restwaerme nach Warmwasserladung: eine Abfrage (6 Ticks) mit Anteil 0,71, danach wieder 0,29.
    states = _trace(_flagged_after_settle(), E, [0.71] + [0.29] * 5, per=6)
    assert all(state.fehlt_seit == F for _, state in states)


def test_the_window_must_span_clear_window_before_a_set_flag_clears():
    states = _trace(_flagged_after_settle(), E, [0.7] * 14)
    assert [ts for ts, state in states if state.fehlt_seit is None][0] == E + CLEAR_WINDOW
    state = dict(states)[E + CLEAR_WINDOW]
    assert state.beobachtung_seit == E + CLEAR_WINDOW and state.unter_schwelle is False and state.anteile == ()
    assert dict(states)[E + CLEAR_WINDOW - TICK].fehlt_seit == F


def test_the_window_keeps_the_last_clear_window_of_shares_plus_one_older_anchor():
    state = _trace(_flagged_after_settle(), E, [0.3] * 30)[-1][1]
    assert len(state.anteile) == 14                                               # 13 im Fenster (60 min inklusive) + Anker
    inside = [ts for ts, _ in state.anteile if state.anteile[-1][0] - ts <= CLEAR_WINDOW]
    assert len(inside) == 13 and state.anteile[-1][0] - state.anteile[0][0] > CLEAR_WINDOW


def test_the_median_of_the_window_decides_with_the_threshold_comparison_on_the_boundary():
    # 13 Werte im Fenster bei E+60: 6 darunter + 7 darueber -> Median darueber -> Entwarnung ...
    states = _trace(_flagged_after_settle(), E, [0.3] * 6 + [0.7] * 7)
    assert states[-1][1].fehlt_seit is None
    # ... aber 7 darunter + 6 darueber -> Median darunter: bleibt, bis die Mehrheit im Fenster darueber liegt (E+65).
    states = dict(_trace(_flagged_after_settle(), E, [0.3] * 7 + [0.7] * 7))
    assert states[E + CLEAR_WINDOW].fehlt_seit == F
    assert states[E + CLEAR_WINDOW + TICK].fehlt_seit is None


def test_a_tick_below_the_threshold_lowers_the_median_instead_of_resetting_the_window():
    states = _trace(_flagged_after_settle(), E, [0.7] * 11 + [0.3, 0.7])
    assert states[-1][1].fehlt_seit is None and states[-1][0] == E + CLEAR_WINDOW


def test_an_interruption_of_the_request_empties_the_window_on_resume():
    state = _trace(_flagged_after_settle(), E, [0.7] * 8)[-1][1]
    assert len(state.anteile) == 8
    state = evaluate(state, E + timedelta(minutes=40), 0.0, 75.0, 21.0)       # Warmwasserladung
    assert len(state.anteile) == 8                                           # die Unterbrechung selbst ist keine Aussage
    resume = E + timedelta(minutes=45)
    state = evaluate(state, resume, 41.0, 35.0, 21.0)                        # Fortsetzung: neue Beruhigung
    assert state.anteile == () and state.fehlt_seit == F
    evaluated = resume + timedelta(minutes=30)
    states = dict(_trace(state, resume + TICK, [0.7] * 19))
    assert states[evaluated + CLEAR_WINDOW - TICK].fehlt_seit == F
    assert states[evaluated + CLEAR_WINDOW].fehlt_seit is None


def test_a_new_phase_after_a_long_pause_empties_the_window_but_keeps_the_flag():
    state = _trace(_flagged_after_settle(), E, [0.7] * 8)[-1][1]
    state = evaluate(state, E + timedelta(minutes=40), 0.0, 75.0, 21.0)
    state = evaluate(state, E + timedelta(minutes=85), 41.0, 35.0, 21.0)     # > 45 min nach dem letzten Anforderungs-Tick
    assert state.anteile == () and state.fehlt_seit == F


def test_ticks_without_a_statement_leave_the_window_alone():
    state = _trace(_flagged_after_settle(), E, [0.7] * 6)[-1][1]
    window = state.anteile
    at = E + timedelta(minutes=30)
    for args in ((41.0, None, 21.0), (41.0, float("nan"), 21.0), (41.0, 33.0, None), (24.0, 22.0, 21.0)):
        state = evaluate(state, at, *args)                                   # fehlender Wert bzw. Hub unter MIN_LIFT
        at += TICK
        assert state.anteile == window and state.fehlt_seit == F


def _jittered_times(start, count, spacing_s, seed=None):
    """Zeitpunkte ab start im Abstand spacing_s Sekunden; mit seed +-5 s deterministische Streuung (Scheduler-Latenz)."""
    rng = random.Random(seed)
    times, ts = [], start
    for _ in range(count):
        times.append(ts)
        ts += timedelta(seconds=spacing_s + (rng.uniform(-5, 5) if seed is not None else 0))
    return times


def _trace_at(state, times, shares, per=1, soll=41.0, room=21.0):
    """Wie _trace, aber mit frei gewaehlten Tick-Zeitpunkten (je Anteil `per` Ticks)."""
    out = []
    for index, ts in enumerate(times):
        value = shares[(index // per) % len(shares)]
        state = evaluate(state, ts, soll, room + value * (soll - room), room)
        out.append((ts, state))
    return out


JITTER = [(301, None), (299, None), (300, 12345), (300, 777)]       # (Abstand in s, Seed fuer +-5 s Streuung)


@pytest.mark.parametrize("spacing, seed", JITTER)
def test_jittered_tick_times_still_clear_a_set_flag_once_a_sample_is_an_hour_old(spacing, seed):
    times = _jittered_times(E, 40, spacing, seed)
    states = _trace_at(_flagged_after_settle(), times, [0.9])
    cleared = _cleared_at(states)
    assert cleared is not None and cleared - times[0] >= CLEAR_WINDOW
    # sofort im ersten Tick, in dem der aelteste Wert eine Stunde alt ist (kein Dauerzustand "haengt")
    first_old = next(ts for ts in times if ts - times[0] >= CLEAR_WINDOW)
    assert cleared == first_old
    assert all(len(state.anteile) <= 14 for _, state in states)           # Fenster + ein Anker, nie mehr


@pytest.mark.parametrize("spacing, seed", JITTER)
def test_a_cycling_boiler_clears_a_set_flag_on_jittered_tick_times(spacing, seed):
    times = _jittered_times(E, 60, spacing, seed)
    states = _trace_at(_flagged_after_settle(), times, [0.79, 0.47], per=6)
    cleared = _cleared_at(states)
    assert cleared is not None and times[0] + CLEAR_WINDOW <= cleared <= times[0] + timedelta(hours=2)


@pytest.mark.parametrize("spacing, seed", JITTER)
def test_a_decaying_residual_heat_trace_never_clears_on_jittered_tick_times(spacing, seed):
    times = _jittered_times(E, 50, spacing, seed)
    states = _trace_at(_flagged_after_settle(), times, [0.6, 0.5, 0.4] + [0.3] * 10, per=6)
    assert _cleared_at(states) is None


def _at_offsets(offsets_s_and_shares):
    state, out = _flagged_after_settle(), []
    for offset_s, value in offsets_s_and_shares:
        state = evaluate(state, E + timedelta(seconds=offset_s), 41.0, 21.0 + value * 20.0, 21.0)
        out.append(state)
    return out


def test_the_anchor_needs_a_sample_of_at_least_the_window_span():
    # Aeltester Wert 59:59 alt -> noch nicht; beim naechsten Tick ist E 64:59 alt und dient als Anker -> Entwarnung.
    states = _at_offsets([(0, 0.9), (1800, 0.9), (3599, 0.9)])
    assert states[-1].fehlt_seit == F
    state = evaluate(states[-1], E + timedelta(seconds=3899), 41.0, 39.0, 21.0)
    assert state.fehlt_seit is None
    # genau eine Stunde alt: zaehlt als Fenster-Wert, nicht als Anker
    states = _at_offsets([(0, 0.9), (1800, 0.9), (3600, 0.9)])
    assert states[-1].fehlt_seit is None


def test_the_anchor_takes_part_in_the_span_check_but_not_in_the_median():
    # Anker E (Anteil 0,9, 61 min alt); im Fenster 0,3 / 0,3 / 0,9 -> Median 0,3. Mit Anker waeren es 0,6: Entwarnung zu Unrecht.
    states = _at_offsets([(0, 0.9), (600, 0.3), (1200, 0.3), (3660, 0.9)])
    assert states[-1].fehlt_seit == F
    assert states[-1].anteile[0][0] == E and len(states[-1].anteile) == 4
    # Spaeter wandert der Anker: E+10 min ist dann eine Stunde alt, E wird verworfen, es bleibt genau ein Anker.
    state = evaluate(states[-1], E + timedelta(seconds=4260), 41.0, 27.0, 21.0)
    assert state.fehlt_seit == F and state.anteile[0][0] == E + timedelta(seconds=600) and len(state.anteile) == 4


@pytest.mark.parametrize("polls", [
    [0.79, 0.47],                   # taktender Brenner: abwechselnd gute und schlechte Abfrage
    [0.47, 0.79],                   # mit der schlechten Abfrage zuerst
    [0.8, 0.8, 0.4],                # zwei gute, eine schlechte
])
def test_a_cycling_but_working_boiler_clears_a_set_flag_within_two_hours(polls):
    states = _trace(_flagged_after_settle(), E, polls * 6, per=6)
    cleared = _cleared_at(states)
    assert cleared is not None and E + CLEAR_WINDOW <= cleared <= E + timedelta(hours=2)
    assert dict(states)[E + timedelta(hours=4)].fehlt_seit is None            # und bleibt geloescht


def test_a_decaying_residual_heat_trace_does_not_clear_the_flag():
    states = _trace(_flagged_after_settle(), E, [0.6, 0.5, 0.4] + [0.3] * 10, per=6)
    assert _cleared_at(states) is None


def _episode(rows, flag_since, end):
    return dict(_replay(rows, end, state=WaermeState(fehlt_seit=flag_since)))


def _on(day, hour, minute):
    return datetime(2026, 9, day, hour, minute, tzinfo=CEST)


# Kalibrierung 2026-09-30 (client1, Verlauf 20.-30.09.): drei Faelle, in denen das Flag frueher faelschlich fiel, obwohl
# die Therme nie heizte (Heiz-Gas 0 kWh). Die Fixtures sind aus den dokumentierten Werten REKONSTRUIERT, keine Rohdaten.
# Aufgezeichnet (Spec 5.2): 21.09. 20:45 Soll 36,5 / Vorlauf 29,0 / Raum 20,8, Anteil 0,52, davor Vorlauf 57,5 -> 52 -> 45
# nach einer Warmwasserladung; 23.09. 21:55 Vorlauf 28,5, Anteil 0,69, Warmwasser-Vorlauf davor 44,5; 26.09. 10:05
# Vorlauf 32,0 gegen 24,5 davor, Anteil 0,71 (Warmwasserladung zwischen zwei ~30-min-Abfragen).
# Gewaehlt/erfunden: Soll 34,7 am 21.09. 20:15, die 21:00-Zeile (Vorlauf 25,0), Soll und Raum am 23.09. (32,0 / 20,8) und
# am 26.09. (36,6 / 20,8; so, dass der dokumentierte Anteil herauskommt), der 26.09.-Vorlauf 24,5 ab 08:00, der Zeitpunkt
# 10:35 (Ende der Abfrage), alle Flag-Zeitpunkte und alle Zeitpunkte der Warmwasser-Zeilen. Die Warmwasser-Zeilen
# (Soll 0) stehen vor der ersten Phase und setzen nur den Zustand auf (ohne laufende Phase ist eine Unterbrechung ohne
# Wirkung); der Unterbrechungspfad selbst ist in den Fenster-Tests oben abgedeckt.
def test_reconstructed_21_09_residual_heat_after_a_hot_water_load_does_not_clear_the_flag():
    rows = [
        (_on(21, 19, 45), 0.0, 57.5, 20.8),      # Warmwasserladung, Vorlauf heiss
        (_on(21, 20, 0), 0.0, 52.0, 20.8),
        (_on(21, 20, 10), 0.0, 45.0, 20.8),
        (_on(21, 20, 15), 34.7, 29.0, 20.8),     # Anforderung wieder da, Vorlauf nur noch Restwaerme
        (_on(21, 20, 30), 36.5, 29.0, 20.8),     # 20:45 (Ende SETTLE): Anteil 0,52 loeschte das Flag
        (_on(21, 21, 0), 36.5, 25.0, 20.8),      # Vorlauf faellt weiter
    ]
    assert share(36.5, 29.0, 20.8) == pytest.approx(0.52, abs=0.01)
    states = _episode(rows, _on(21, 9, 25), _on(21, 22, 0))
    assert states[_on(21, 20, 45)].fehlt_seit == _on(21, 9, 25)
    assert len(states[_on(21, 20, 45)].anteile) == 1
    assert all(state.fehlt_seit == _on(21, 9, 25) for state in states.values())


def test_reconstructed_23_09_residual_heat_after_hot_water_does_not_clear_the_flag():
    rows = [
        (_on(23, 21, 10), 0.0, 44.5, 20.8),      # Warmwasserladung
        (_on(23, 21, 25), 32.0, 28.5, 20.8),     # 21:55 (Ende SETTLE): Anteil 0,69 loeschte das Flag
        (_on(23, 22, 10), 32.0, 24.0, 20.8),
    ]
    assert share(32.0, 28.5, 20.8) == pytest.approx(0.69, abs=0.01)
    states = _episode(rows, _on(22, 9, 20), _on(23, 23, 30))
    assert all(state.fehlt_seit == _on(22, 9, 20) for state in states.values())


def test_reconstructed_26_09_a_hot_water_load_hidden_between_two_polls_does_not_clear_the_flag():
    rows = [
        (_on(26, 8, 0), 36.6, 24.5, 20.8),
        (_on(26, 10, 5), 36.6, 32.0, 20.8),      # ein Abfragewert, Anteil 0,71, loeschte das Flag
        (_on(26, 10, 35), 36.6, 24.5, 20.8),     # naechste Abfrage ~30 min spaeter: wieder 24,5
    ]
    assert share(36.6, 32.0, 20.8) == pytest.approx(0.71, abs=0.01)
    states = _episode(rows, _on(24, 3, 10), _on(26, 12, 0))
    assert all(state.fehlt_seit == _on(24, 3, 10) for state in states.values())


def test_parse_since_accepts_only_iso_text_with_a_time_zone():
    assert parse_since("2026-09-30T05:11:00+02:00") == _at(5, 11)
    for bad in (None, 5, "", "gestern", "2026-09-30T05:11:00"):
        assert parse_since(bad) is None
