from datetime import datetime

import pytest
from fakes import LaggingFakeHa, WallClock
from test_runtime import (  # noqa: F401  (env ist eine Fixture)
    _advance,
    _answer,
    _backup,
    _connect,
    _delivery,
    _mqtt,
    _quiet_backup,
    _set_room_target,
    _start,
    _trigger,
    env,
)

from heizungsbruecke import __main__ as main_module


@pytest.fixture
def lagging(env):
    """env mit dem realistischeren HA-Modell; vor _start() einsetzen."""
    env.ha = LaggingFakeHa(env.clock)
    return env.ha


def test_lagging_fake_shows_a_written_value_only_after_the_lag(env, lagging):
    lagging.set_number_value("number.curve_current", 1.2)
    assert lagging.get_state("number.curve_current") == 0.9
    env.clock.advance(1799)
    assert lagging.get_state("number.curve_current") == 0.9
    env.clock.advance(1)
    assert lagging.get_state("number.curve_current") == 1.2


def test_lagging_fake_unavailable_entity_swallows_the_call(env, lagging):
    lagging.states["number.curve_current"] = "unavailable"
    lagging.set_number_value("number.curve_current", 1.2)
    assert lagging.writes == [] and lagging.service_calls == [("number.curve_current", 1.2)]


def test_lagging_fake_quota_blocks_with_403(env, lagging):
    lagging.quota_until = env.clock() + 100
    with pytest.raises(RuntimeError, match="403"):
        lagging.set_number_value("number.curve_current", 1.2)
    assert lagging.quota_attempts == 1
    env.clock.advance(100)
    lagging.set_number_value("number.curve_current", 1.2)
    assert lagging.writes == [("number.curve_current", 1.2)]


def test_lagging_fake_set_hvac_mode_respects_write_error(env, lagging):
    lagging.write_error = RuntimeError("HA nicht erreichbar")
    with pytest.raises(RuntimeError):
        lagging.set_hvac_mode("climate.zone", "heat_cool")
def _curve_calls(ha):
    return [call for call in ha.service_calls if call[0] == "number.curve_current"]


def test_answer_for_an_unavailable_entity_writes_nothing_and_is_retried(env, lagging):
    # AU-016: HA wuerde den Aufruf still mit 200 ueberspringen; das Add-on darf den Wert nicht fuer geschrieben halten.
    _quiet_backup(env)
    bridge = _start(env)
    _connect(env, bridge)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[0]["seq"]
    lagging.states["number.curve_current"] = "unavailable"

    _answer(env, bridge, seq, curve=0.95, shift=23.0)

    assert _curve_calls(lagging) == []  # kein Cloud-Aufruf an eine nicht verfuegbare Entity
    assert _delivery(bridge).pending.seq == seq  # nicht quittiert, wird mit derselben seq wiederholt

    lagging.states["number.curve_current"] = 0.9  # die Entity ist wieder da
    _advance(env, bridge, 300)
    _trigger(env, bridge, "sensor.room_actual")
    assert [snapshot["seq"] for snapshot in _mqtt(env).snapshots] == [seq, seq]  # Wiederholung mit derselben seq
    _answer(env, bridge, seq, curve=0.95, shift=23.0)
    assert len(_curve_calls(lagging)) == 1  # jetzt wird der Wert geschrieben ...
    assert _delivery(bridge).pending is None  # ... und der Tick ist erledigt


def test_failing_boost_end_does_not_hammer_the_quota(env, lagging):
    # AU-015: sieben lokale Checks in sieben Minuten duerfen hoechstens zwei Cloud-Versuche ausloesen
    # (sofort und nach 300 s, Rueckkehr-Staffel 5/15/30 min). Das Boost-Ende kommt ueber die Ankunftsschwelle
    # (Raum 21.6 bei Soll 22.0, wie test_runtime::test_restart_during_comfort_boost_continues_and_ends_on_arrival);
    # min_flow steht schon auf dem Raum-Soll, damit jeder Versuch ein Boost-Ende-Versuch ist.
    _quiet_backup(env, boost_active=True, last_room_target=22.0, last_published_target_rt=22.0)
    lagging.states.update({
        "sensor.room_target": 22.0, "number.min_flow": 22.0, "number.curve_current": 1.5, "number.shift_current": 25.0,
    })
    lagging.quota_until = env.clock() + 7200
    bridge = _start(env)
    assert lagging.quota_attempts == 0  # der Boost laeuft weiter, nichts zu schreiben
    lagging.states["sensor.room_actual"] = 21.6  # Ankunft: der Boost soll enden
    for _ in range(7):
        _trigger(env, bridge, "sensor.room_actual")
        _advance(env, bridge, 60)
    assert 1 <= lagging.quota_attempts <= 2  # mindestens ein Versuch (Test ist nicht leer), hoechstens zwei
    assert _backup(env)["boost_active"] is True  # nicht beendet, die Rueckkehr-Staffel versucht es spaeter


def test_a_written_value_is_not_rewritten_while_ha_still_shows_the_old_one(env, lagging):
    _quiet_backup(env)
    bridge = _start(env)
    _connect(env, bridge)
    _set_room_target(env, bridge, 20.5)
    _answer(env, bridge, _mqtt(env).snapshots[0]["seq"], curve=0.95, shift=23.0)
    assert len(_curve_calls(lagging)) == 1

    for _ in range(3):
        _advance(env, bridge, 300)
        _trigger(env, bridge, "sensor.room_actual")

    assert lagging.get_state("number.curve_current") == 0.9  # HA hinkt noch nach (Lag 1800 s)
    assert len(_curve_calls(lagging)) == 1  # kein zweiter Cloud-Aufruf fuer denselben Wert

    _advance(env, bridge, 1000)  # insgesamt > 1800 s: HA zeigt jetzt den geschriebenen Wert
    _trigger(env, bridge, "sensor.room_actual")
    assert lagging.get_state("number.curve_current") == 0.95
    assert len(_curve_calls(lagging)) == 1


def test_climate_zone_start_switches_the_mode_once_despite_the_mode_lag(env, lagging):
    lagging.states.update({"climate.zone": "auto", "climate.zone::temperature": 0.0})
    _quiet_backup(env)
    bridge = _start(env, entity_shift_current="climate.zone")
    for _ in range(6):  # 720 s: laenger als der Modus-Lag (600 s), am Ende zeigt die Zone "heat_cool"
        _advance(env, bridge, 120)
        _trigger(env, bridge, "sensor.room_actual")
    assert lagging.states["climate.zone"] == "heat_cool"
    mode_calls = [call for call in lagging.service_calls if call == ("climate.zone", "heat_cool")]
    assert len(mode_calls) == 1  # der Modus zeigt noch "auto" (Lag 600 s), das Add-on stellt nicht erneut um


@pytest.fixture
def wall(monkeypatch):
    clock = WallClock(datetime(2026, 3, 28, 11, 59, tzinfo=WallClock.ZONE))
    monkeypatch.setattr(main_module, "datetime", clock.datetime_class)
    return clock


def _daily_snapshots(env):
    return [snapshot for snapshot in _mqtt(env).snapshots if snapshot["trigger"] == "daily"]


def test_daily_tick_fires_once_per_calendar_day_across_midnight_and_the_dst_change(env, wall):
    # 2026-03-29: Zeitumstellung (02:00 -> 03:00 Europe/Berlin), Tagestick um 12:00.
    _quiet_backup(env, last_daily_trigger_date="2026-03-27")
    bridge = _start(env)
    _connect(env, bridge)

    def check_at(day, hour, minute):
        wall.set(datetime(2026, 3, day, hour, minute, tzinfo=wall.ZONE))
        _trigger(env, bridge, "sensor.room_actual")

    check_at(28, 11, 59)
    assert _daily_snapshots(env) == []  # 11:59, noch nicht faellig
    check_at(28, 12, 0)
    assert len(_daily_snapshots(env)) == 1  # 12:00 -> genau ein Tick
    _answer(env, bridge, _daily_snapshots(env)[0]["seq"])
    check_at(29, 0, 1)
    assert len(_daily_snapshots(env)) == 1  # nach Mitternacht nicht erneut
    wall.set(datetime(2026, 3, 29, 1, 30, tzinfo=wall.ZONE))
    wall.advance(3600)  # absolut eine Stunde weiter: ueber die Umstellung 02:00 -> 03:00, lokal 03:30
    assert wall.datetime_class.now().hour == 3
    _trigger(env, bridge, "sensor.room_actual")
    assert len(_daily_snapshots(env)) == 1  # auch die Umstellung loest keinen Tick aus
    check_at(29, 11, 59)
    assert len(_daily_snapshots(env)) == 1  # Tag der Zeitumstellung, vor 12:00 nichts
    check_at(29, 12, 0)
    assert len(_daily_snapshots(env)) == 2  # 12:00 am Tag der Zeitumstellung, genau ein weiterer Tick
