from datetime import datetime, timedelta
from pathlib import Path

from heizungsbruecke.backup_store import load_backup, save_backup

# Wie zuvor mit den hartcodierten 20:00/08:00-Grenzen: ein first-boot-Fenster von 2h
# nach der konfigurierten Fenstergrenze, in dem noch nachgeholt wird, solange keine
# Kontinuitaet (last_checked) bekannt ist. Siehe maybe_snapshot()-Docstring.
_COLD_START_CATCHUP = timedelta(hours=2)


def _parse_boundary(now: datetime, hhmm: str) -> datetime:
    parsed = datetime.strptime(hhmm, "%H:%M").time()
    return now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)


def maybe_snapshot(
    ha_api, room_12h_avg_entity_id: str, day_avg_entity_id: str, night_avg_entity_id: str,
    day_avg_window_end: str, night_avg_window_end: str, state_path: Path, now: datetime,
) -> None:
    """Snapshots the rolling room average into the day/night input_numbers once per day.

    `day_avg_window_end`/`night_avg_window_end` ("HH:MM") come from the tenant's profile
    (Design-Spec 2026-09-16, Abschnitt B) instead of the previously hardcoded 20:00/08:00
    -- the mechanism below (boundary-crossing-since-last-check, narrow cold-start
    catch-up) is unchanged, only the concrete times are parameters now.

    `local_check_interval_seconds` (config.yaml) is a customer-configurable, but now
    HARD-CAPPED-AT-60s interval -- a narrow fixed clock-hour window (e.g. "only between
    20:00 and 22:00") can permanently miss the snapshot every single day if the tick
    phase never lands inside it. So once there is a recorded `last_checked` from a
    previous call, "due" is decided by whether the window-end boundary was crossed
    *since that last call* -- this fires on the very next call after the boundary no
    matter how long the check interval is, guaranteeing exactly one snapshot per day.

    Without a recorded `last_checked` (state file missing/fresh, e.g. first-ever run),
    there is no continuity to reason from -- falling back to unconditional "boundary
    already passed today" would also fire hours-late snapshots using a rolling average
    already contaminated by the other half of the day. For that cold-start case only,
    fall back to a narrow catch-up window and skip today's snapshot if we're already
    past it.

    Unlike the pre-decoupling version, this function is now called from a fast local
    loop (as low as every 30s, see Design-Spec Abschnitt A) -- an unconditional write
    on every call would wear the SD card the same way the old backup.json
    write-every-tick behaviour did. The state file is therefore only written when a
    snapshot actually fires, or on the very first call ever (to escape cold-start
    permanently) -- see the `changed` bookkeeping below.
    """
    state = load_backup(state_path)
    today = now.date().isoformat()
    last_checked_raw = state.get("last_checked")
    last_checked = datetime.fromisoformat(last_checked_raw) if last_checked_raw else None

    day_boundary = _parse_boundary(now, day_avg_window_end)
    night_boundary = _parse_boundary(now, night_avg_window_end)

    if last_checked is not None:
        day_due = last_checked < day_boundary <= now
        night_due = last_checked < night_boundary <= now
    else:
        day_due = day_boundary <= now < day_boundary + _COLD_START_CATCHUP
        night_due = night_boundary <= now < night_boundary + _COLD_START_CATCHUP

    changed = False

    if day_due and state.get("last_day_snapshot_date") != today:
        value = ha_api.get_state(room_12h_avg_entity_id)
        ha_api.set_input_number_value(day_avg_entity_id, value)
        state["last_day_snapshot_date"] = today
        changed = True

    if night_due and state.get("last_night_snapshot_date") != today:
        value = ha_api.get_state(room_12h_avg_entity_id)
        ha_api.set_input_number_value(night_avg_entity_id, value)
        state["last_night_snapshot_date"] = today
        changed = True

    if changed or last_checked is None:
        state["last_checked"] = now.isoformat()
        save_backup(state_path, state)
