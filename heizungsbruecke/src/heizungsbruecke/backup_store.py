import json
import os
from pathlib import Path


def save_backup(path: Path, values: dict) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(values))
    os.replace(tmp_path, path)


def load_backup(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())
