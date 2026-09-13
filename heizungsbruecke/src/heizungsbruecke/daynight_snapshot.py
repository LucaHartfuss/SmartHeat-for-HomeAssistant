from datetime import datetime
from pathlib import Path

from heizungsbruecke.backup_store import load_backup, save_backup


def maybe_snapshot(
    ha_api, room_12h_avg_entity_id: str, day_avg_entity_id: str, night_avg_entity_id: str,
    state_path: Path, now: datetime,
) -> None:
    state = load_backup(state_path)
    today = now.date().isoformat()

    if now.hour >= 20 and state.get("last_day_snapshot_date") != today:
        value = ha_api.get_state(room_12h_avg_entity_id)
        ha_api.set_input_number_value(day_avg_entity_id, value)
        state["last_day_snapshot_date"] = today
        save_backup(state_path, state)

    # Night snapshot is only valid during daytime hours (8:00-19:59).
    # The 12h rolling average reflects the just-finished night (20:00→08:00) correctly only
    # before 20:00; after 20:00 the rolling average includes daytime data and is no longer
    # a valid representation of "last night's temperature". Late first boots must wait until
    # the next 08:00 for the night snapshot to be meaningful.
    if 8 <= now.hour < 20 and state.get("last_night_snapshot_date") != today:
        value = ha_api.get_state(room_12h_avg_entity_id)
        ha_api.set_input_number_value(night_avg_entity_id, value)
        state["last_night_snapshot_date"] = today
        save_backup(state_path, state)
