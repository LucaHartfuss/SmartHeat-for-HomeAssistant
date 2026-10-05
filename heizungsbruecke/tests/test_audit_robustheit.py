"""AUDIT 2026-09-30 (Teilsystem B): Belege fuer Befunde aus dem Code-Audit. Nur Tests, keine
Aenderung am Produktionscode. Ein mit xfail(strict=True) markierter Test beschreibt das
erwartete Verhalten und belegt, dass der heutige Code es NICHT zeigt.

Nutzt das Harness aus test_runtime.py (Fake-HA mit sofort sichtbaren Schreibwerten, Fake-MQTT,
Fake-Uhr)."""
from datetime import date, timedelta

from test_runtime import (
    _advance,
    _answer,
    _break_backup_writes,
    _delivery,
    _mqtt,
    _quiet_backup,
    _set_room_target,
    _start,
    _trigger,
    env,  # noqa: F401  (registriert die Fixture `env`)
)

from smartheat_runtime import entitlement
from smartheat_runtime.backup_store import save_backup
from smartheat_runtime.state import StateStore


def test_unwritable_disk_no_longer_blocks_ticks_silently(env, monkeypatch):
    _quiet_backup(env)
    bridge = _start(env)
    pushes, persistent = len(env.ha.pushes), len(env.ha.persistent)
    status_before = env.ha.events[-1][1]["status"]
    _break_backup_writes(monkeypatch)

    _set_room_target(env, bridge, 20.5)  # Soll-Aenderung -> target_change-Tick waere faellig
    _advance(env, bridge, 300)

    sent = len(_mqtt(env).snapshots) > 0
    reported = len(env.ha.pushes) > pushes or len(env.ha.persistent) > persistent
    status_changed = env.ha.events[-1][1]["status"] != status_before
    assert sent or reported or status_changed


def test_unwritable_disk_on_answer_is_not_diagnosed_as_server_outage(env, monkeypatch):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[0]["seq"]
    _break_backup_writes(monkeypatch)

    _answer(env, bridge, seq)  # Server antwortet, apply_server_values wirft beim Speichern
    _advance(env, bridge, 30)  # 1. Ack-Timeout -> sofortiger Retry derselben seq
    _answer(env, bridge, seq)  # Server antwortet erneut (idempotent)
    _advance(env, bridge, 30)  # 2. Ack-Timeout -> Abo-Abfrage (aktiv) -> Notbetrieb

    assert _delivery(bridge).notbetrieb is False


def test_unreadable_room_target_at_start_no_longer_suppresses_the_due_daily_tick(env):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    _quiet_backup(env, last_daily_trigger_date=yesterday)
    env.ha.states["sensor.room_target"] = ValueError("could not convert string to float: 'unavailable'")

    bridge = _start(env, daily_trigger_time="00:00")  # Tagestick heute faellig
    _trigger(env, bridge, "sensor.room_actual")
    _advance(env, bridge, 300)  # Watchdog/Health-Takt

    attempted = len(_mqtt(env).snapshots) > 0 or _delivery(bridge).pending is not None
    assert attempted or _delivery(bridge).datenfehler is not None


def test_reactivated_abo_restart_no_longer_keeps_notbetrieb_until_next_tick(env):
    _quiet_backup(env)
    save_backup(env.paths["FAILSAFE_PATH"], {"failsafe_active": True, "datenfehler": None, "pending": None})
    env.abo["status"] = entitlement.ACTIVE

    bridge = _start(env)
    _advance(env, bridge, 300)

    ended = _delivery(bridge).notbetrieb is False
    tick_started = len(_mqtt(env).snapshots) > 0
    assert ended or tick_started


def _count_curve_write_attempts(env):
    attempts = []
    original = env.ha.set_number_value

    def _failing(entity_id, value):
        if entity_id == "number.curve_current":
            attempts.append(value)
            raise RuntimeError("403 Quota Exceeded (G006)")
        return original(entity_id, value)

    env.ha.set_number_value = _failing
    return attempts


def test_failing_boost_end_write_follows_the_return_staircase(env):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 22.0)  # Comfort-Boost startet (1.5/25) + Tick
    _answer(env, bridge, _mqtt(env).snapshots[-1]["seq"], curve=0.9, shift=22.0)  # nur gespeichert
    assert bridge.store.state.boost_active is True
    attempts = _count_curve_write_attempts(env)

    for value in (21.6, 21.7, 21.8, 21.9, 22.0, 21.9):  # Raum angekommen -> Boost-Ende faellig
        env.ha.states["sensor.room_actual"] = value
        _advance(env, bridge, 60)
        _trigger(env, bridge, "sensor.room_actual")

    assert len(attempts) <= 2  # Rueckkehr-Staffel: sofort, dann fruehestens nach 300 s (vorher: 6)


def test_old_manual_override_format_is_dropped_by_the_first_backup_write(tmp_path):
    """AUDIT, Neubewertung B-TP11-3: das alte Format ({curve, offset, erkannt}) wird beim Laden
    verworfen (WARNING) und verschwindet mit dem ersten Schreiben von backup.json aus der Datei;
    die WARNINGs enden damit nach dem ersten Schreibanlass von selbst."""
    old = {"curve": 1.5, "offset": 20.0, "erkannt": "2026-09-28T10:00:00+02:00"}
    save_backup(tmp_path / "backup.json", {
        "curve_current": 1.5, "shift_current": 20.5, "manual_override": old, "manual_override_pending": old,
    })
    store = StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json")
    assert store.state.manual_override is None

    store.update(last_ack_at="2026-09-30T12:00:05+02:00")  # z. B. die naechste Serverantwort

    reloaded = StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json")
    raw = (tmp_path / "backup.json").read_text()
    assert "manual_override" not in raw
    assert reloaded.state.restore_point.get("curve") == 1.5  # alter Rollen-Schluessel, migriert (Plan 2)
