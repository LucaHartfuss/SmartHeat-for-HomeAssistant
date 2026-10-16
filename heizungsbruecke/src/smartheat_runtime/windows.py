"""Tagestick-Zeit aus dem Konfigurationsdokument (TP11: Tag-/Nachtfenster entfallen)."""
import re

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def parse_hhmm_minutes(value) -> int:
    if not isinstance(value, str) or not _HHMM_RE.fullmatch(value):
        raise ValueError(f"{value!r} ist keine Uhrzeit im Format HH:MM")
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def validate_daily_trigger_time(value) -> str:
    """HH:MM wie profiles._hhmm_minutes im Server. Wirft ValueError."""
    if value is None or value == "":
        raise ValueError("Tagestick fehlt")
    try:
        parse_hhmm_minutes(value)
    except ValueError as error:
        raise ValueError(f"Tagestick: {error}") from None
    return value
