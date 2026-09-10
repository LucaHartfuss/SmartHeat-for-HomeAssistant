import json
from pathlib import Path


def save_backup(path: Path, values: dict) -> None:
    path.write_text(json.dumps(values))


def load_backup(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())
