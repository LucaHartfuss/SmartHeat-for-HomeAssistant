from datetime import datetime
from pathlib import Path

from heizungsbruecke.backup_store import load_backup, save_backup


def maybe_snapshot(
    ha_api, room_12h_avg_entity_id: str, day_avg_entity_id: str, night_avg_entity_id: str,
    state_path: Path, now: datetime,
) -> None:
    state = load_backup(state_path)
    today = now.date().isoformat()

    # Day snapshot is only valid in a narrow catch-up window right after 20:00.
    # The 12h rolling average reflects the just-finished day (08:00->20:00) correctly only
    # right at 20:00; every hour that passes afterwards mixes in night data, so a boot that
    # first sees this code at e.g. 23:00 must skip today's day snapshot rather than record a
    # night-contaminated value under the "day" label. The window is sized to the default
    # hourly poll interval, not to "any time before the next boundary".
    if 20 <= now.hour < 22 and state.get("last_day_snapshot_date") != today:
        value = ha_api.get_state(room_12h_avg_entity_id)
        ha_api.set_input_number_value(day_avg_entity_id, value)
        state["last_day_snapshot_date"] = today
        save_backup(state_path, state)

    # Night snapshot is only valid in a narrow catch-up window right after 08:00, for the
    # symmetric reason: the 12h rolling average reflects the just-finished night
    # (20:00->08:00) correctly only right at 08:00, and degrades with every passing hour as
    # daytime data mixes in. Late first boots skip today's night snapshot and wait for the
    # next 08:00 rather than record a day-contaminated value under the "night" label.
    if 8 <= now.hour < 10 and state.get("last_night_snapshot_date") != today:
        value = ha_api.get_state(room_12h_avg_entity_id)
        ha_api.set_input_number_value(night_avg_entity_id, value)
        state["last_night_snapshot_date"] = today
        save_backup(state_path, state)
