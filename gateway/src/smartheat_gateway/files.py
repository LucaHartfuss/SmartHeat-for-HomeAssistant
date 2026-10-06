"""Atomare Dateien (Spec SHG G2 6.1): temporaere Datei, fsync, rename, fsync des Ordners. Geheimnisse 0600."""
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def _write_bytes(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.fchmod(fd, mode)
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def write_json(path: Path, data, *, private: bool = False) -> None:
    _write_bytes(path, json.dumps(data, ensure_ascii=False, sort_keys=True).encode(), 0o600 if private else 0o644)


def write_text_private(path: Path, text: str) -> None:
    _write_bytes(path, text.encode(), 0o600)


def read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        logger.warning("%s nicht lesbar (%s), gilt als leer", path, type(error).__name__)
        return None
