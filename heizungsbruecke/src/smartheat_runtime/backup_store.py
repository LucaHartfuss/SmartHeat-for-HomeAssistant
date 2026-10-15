import json
from pathlib import Path

from smartheat_core import dateien


def save_backup(path: Path, values: dict) -> None:
    """Atomar und auf dem Datentraeger (fsync, AU-035) ueber den einen Schreibweg; das Format bleibt json.dumps ohne
    sort_keys (der Golden Master haengt an der Reihenfolge)."""
    dateien.schreiben(path, json.dumps(values).encode(), modus=dateien.PRIVAT)


def load_backup(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())
