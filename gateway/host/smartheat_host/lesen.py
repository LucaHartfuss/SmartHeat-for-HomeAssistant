"""Begrenztes, sicheres Lesen von Dateien, die ein Container schreiben kann (Sicherheit): die Host-Dienste
laufen als root, ${SHG_ROOT}/data gehoert dem Container-Benutzer (uid 1000). Eine untergeschobene Datei darf weder per
Symlink auf Fremdes zeigen (O_NOFOLLOW), noch den Dienst an einer FIFO festhalten (O_NONBLOCK, nur regulaere Dateien),
noch den Speicher fuellen (hoechstens `cap` Bytes). Jeder Fehler ist ein OSError (UnsichereDatei fuer die Faelle,
die das Betriebssystem nicht selbst meldet); Meldungen nennen nur den Typ, nie den Inhalt."""
import json
import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_BYTES = 65536  # wie wire.MAX_BODY_BYTES: Konfiguration, Zustand und Meldungen sind klein


class UnsichereDatei(OSError):
    """Keine regulaere Datei oder zu gross."""


def read_stat(path: Path, cap: int = MAX_BYTES) -> tuple[bytes, os.stat_result]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)  # Symlink: ELOOP; FIFO: blockiert nicht
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise UnsichereDatei("keine regulaere Datei")
        if info.st_size > cap:
            raise UnsichereDatei("zu gross")
        data = b""
        while len(data) <= cap:
            chunk = os.read(fd, cap + 1 - len(data))
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    if len(data) > cap:
        raise UnsichereDatei("zu gross")
    return data, info


def read_bytes(path: Path, cap: int = MAX_BYTES) -> bytes:
    return read_stat(path, cap)[0]


def read_json(path: Path, cap: int = MAX_BYTES):
    """Geparster Inhalt; fehlt die Datei oder ist sie unsicher/unlesbar/kein JSON, gilt sie als leer (None)."""
    try:
        return json.loads(read_bytes(path, cap))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        logger.warning("%s nicht lesbar (%s), gilt als leer", path.name, type(error).__name__)
        return None
