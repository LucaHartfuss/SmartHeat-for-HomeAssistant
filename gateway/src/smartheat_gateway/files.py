"""Atomares Schreiben nur fuer den Host-Dienst (root, Paket smartheat_host ohne smartheat_core; install.sh legt diese
Datei mit ab). Der Gateway-Container und der Geraetekern schreiben ueber smartheat_core.dateien (A5-60); die Logik hier
ist dieselbe, nur ohne Aufrufer im Container."""
import contextlib
import json
import os
from pathlib import Path


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
