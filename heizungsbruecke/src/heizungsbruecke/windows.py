"""Tag-/Nachtfenster und Tagestick-Zeit aus den Add-on-Optionen. Der Server liefert sie bei
der Provisionierung (`profile_params`), die Integration schreibt sie als Optionen. Gleiche
Regeln wie `validate_trigger_windows` im Server (generic/profiles.py)."""
import re
from dataclasses import dataclass

WINDOW_OPTION_KEYS = (
    "daily_trigger_time",
    "day_avg_window_start", "day_avg_window_end",
    "night_avg_window_start", "night_avg_window_end",
)

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class WindowDefaults:
    """Tag- und Nachtfenster muessen gleich gross sein: beide werden vom selben rollierenden
    statistics-Sensor gelesen (derived_sensors.py), ein max_age_hours-Wert bedient beide."""

    daily_trigger_time: str
    day_avg_window_start: str
    day_avg_window_end: str
    night_avg_window_start: str
    night_avg_window_end: str


def parse_hhmm_minutes(value) -> int:
    if not isinstance(value, str) or not _HHMM_RE.fullmatch(value):
        raise ValueError(f"{value!r} ist keine Uhrzeit im Format HH:MM")
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def window_size_hours(start: str, end: str) -> float:
    return (parse_hhmm_minutes(end) - parse_hhmm_minutes(start)) / 60.0


def check_window_invariants(windows: WindowDefaults) -> None:
    day_size = window_size_hours(windows.day_avg_window_start, windows.day_avg_window_end)
    night_size = window_size_hours(windows.night_avg_window_start, windows.night_avg_window_end)
    if day_size <= 0:
        raise ValueError(
            f"Tagesfenster ({windows.day_avg_window_start}-{windows.day_avg_window_end}) ist nicht positiv"
        )
    if night_size <= 0:
        raise ValueError(
            f"Nachtfenster ({windows.night_avg_window_start}-{windows.night_avg_window_end}) ist nicht positiv"
        )
    if day_size != night_size:
        raise ValueError(
            f"Tagesfenster ({day_size}h) und Nachtfenster ({night_size}h) muessen gleich gross "
            f"sein (gemeinsamer statistics-Sensor)"
        )


def windows_from_options(options: dict) -> WindowDefaults:
    values = {}
    for key in WINDOW_OPTION_KEYS:
        value = options.get(key)
        if value is None or value == "":
            raise ValueError(f"Option '{key}' fehlt")
        try:
            parse_hhmm_minutes(value)
        except ValueError as error:
            raise ValueError(f"Option '{key}': {error}") from None
        values[key] = value
    windows = WindowDefaults(**values)
    check_window_invariants(windows)
    return windows
