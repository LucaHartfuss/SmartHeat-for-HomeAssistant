"""Tagestick-Zeit aus den Add-on-Optionen (TP11: Tag-/Nachtfenster entfallen)."""
import re

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def parse_hhmm_minutes(value) -> int:
    if not isinstance(value, str) or not _HHMM_RE.fullmatch(value):
        raise ValueError(f"{value!r} ist keine Uhrzeit im Format HH:MM")
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def validate_daily_trigger_time(value) -> str:
    """HH:MM wie profiles.validate_daily_trigger_time im Server. Wirft ValueError."""
    if value is None or value == "":
        raise ValueError("Option 'daily_trigger_time' fehlt")
    try:
        parse_hhmm_minutes(value)
    except ValueError as error:
        raise ValueError(f"Option 'daily_trigger_time': {error}") from None
    return value
