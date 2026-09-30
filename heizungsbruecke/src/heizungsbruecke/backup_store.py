import json
import os
from pathlib import Path


def save_backup(path: Path, values: dict) -> None:
    """Atomar: temporaere Datei schreiben und auf den Datentraeger bringen (fsync), dann umbenennen.
    Ohne fsync kann nach einem Stromausfall eine leere Datei unter dem alten Namen stehen (AU-035)."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w") as handle:
        handle.write(json.dumps(values))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def load_backup(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())
