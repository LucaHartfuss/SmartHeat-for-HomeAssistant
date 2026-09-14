from datetime import datetime
from pathlib import Path

from heizungsbruecke.backup_store import load_backup, save_backup


def maybe_snapshot(
    ha_api, room_12h_avg_entity_id: str, day_avg_entity_id: str, night_avg_entity_id: str,
    state_path: Path, now: datetime,
) -> None:
    """Snapshots the 12h rolling room average into the day/night input_numbers once per day.

    `poll_interval_seconds` (config.yaml) is a customer-configurable, unbounded loop
    interval -- a narrow fixed clock-hour window (e.g. "only between 20:00 and 22:00")
    can permanently miss the snapshot every single day if the tick phase never lands
    inside it (e.g. a 3h poll interval that always ticks at :xx+1 past the window).
    So once there is a recorded `last_checked` from a previous call, "due" is decided
    by whether the 20:00/08:00 boundary was crossed *since that last call* -- this
    fires on the very next tick after the boundary no matter how long the poll
    interval is, guaranteeing exactly one snapshot per day.

    Without a recorded `last_checked` (state file missing/fresh, e.g. first-ever run),
    there is no continuity to reason from -- falling back to unconditional "boundary
    already passed today" would also fire hours-late snapshots using a rolling average
    already contaminated by the other half of the day. For that cold-start case only,
    fall back to the original narrow catch-up window (sized to the default hourly poll
    interval) and skip today's snapshot if we're already past it.
    """
    state = load_backup(state_path)
    today = now.date().isoformat()
    last_checked_raw = state.get("last_checked")
    last_checked = datetime.fromisoformat(last_checked_raw) if last_checked_raw else None

    day_boundary = now.replace(hour=20, minute=0, second=0, microsecond=0)
    night_boundary = now.replace(hour=8, minute=0, second=0, microsecond=0)

    if last_checked is not None:
        day_due = last_checked < day_boundary <= now
        night_due = last_checked < night_boundary <= now
    else:
        day_due = 20 <= now.hour < 22
        night_due = 8 <= now.hour < 10

    if day_due and state.get("last_day_snapshot_date") != today:
        value = ha_api.get_state(room_12h_avg_entity_id)
        ha_api.set_input_number_value(day_avg_entity_id, value)
        state["last_day_snapshot_date"] = today

    if night_due and state.get("last_night_snapshot_date") != today:
        value = ha_api.get_state(room_12h_avg_entity_id)
        ha_api.set_input_number_value(night_avg_entity_id, value)
        state["last_night_snapshot_date"] = today

    state["last_checked"] = now.isoformat()
    save_backup(state_path, state)
