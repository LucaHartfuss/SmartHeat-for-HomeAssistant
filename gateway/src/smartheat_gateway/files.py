"""Atomare Dateien (Spec SHG G2 6.1): temporaere Datei, fsync, rename, fsync des Ordners. Geheimnisse 0600."""
import contextlib
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def _write_bytes(path: Path, data: bytes, mode: int) -> None:
    """Atomar und ohne Symlinks zu folgen: der Host-Dienst (root) schreibt in Ordner, in denen ein Container (uid 1000)
    Dateien und Symlinks anlegen kann. Der Ordner wird mit O_NOFOLLOW geoeffnet, eine alte temporaere Datei (auch ein
    untergeschobener Symlink) per dir_fd entfernt, die neue mit O_EXCL|O_NOFOLLOW angelegt, und rename ersetzt einen
    Symlink am Zielnamen, statt ihm zu folgen."""
    path.parent.mkdir(parents=True, exist_ok=True)
    dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        tmp = path.name + ".tmp"
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=dir_fd)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=dir_fd)
        try:
            os.fchmod(fd, mode)
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
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
