from datetime import datetime
from pathlib import Path

from heizungsbruecke.backup_store import load_backup, save_backup


def maybe_snapshot(
    ha_api, room_12h_avg_entity_id: str, day_avg_entity_id: str, night_avg_entity_id: str,
    state_path: Path, now: datetime,
) -> None:
    state = load_backup(state_path)
    today = now.date().isoformat()

    # Capture whether both are undone before any modifications
    both_undone_at_start = state.get("last_day_snapshot_date") != today and state.get("last_night_snapshot_date") != today

    if now.hour >= 20 and state.get("last_day_snapshot_date") != today:
        value = ha_api.get_state(room_12h_avg_entity_id)
        ha_api.set_input_number_value(day_avg_entity_id, value)
        state["last_day_snapshot_date"] = today
        save_backup(state_path, state)

    # Night can be recorded in the morning (8-20) or late evening (21+) on first startup
    night_can_be_recorded = (now.hour >= 8 and now.hour < 20) or (
        now.hour >= 21 and both_undone_at_start
    )
    if night_can_be_recorded and state.get("last_night_snapshot_date") != today:
        value = ha_api.get_state(room_12h_avg_entity_id)
        ha_api.set_input_number_value(night_avg_entity_id, value)
        state["last_night_snapshot_date"] = today
        save_backup(state_path, state)
