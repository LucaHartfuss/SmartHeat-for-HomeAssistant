"""Der eine atomare Schreibweg (Audit 5, A5-60; Spec Stufe 3 S3 Abschnitt 7.1), rein (Standardbibliothek): temporaere
Datei mit O_EXCL|O_NOFOLLOW im per O_NOFOLLOW geoeffneten Ordner, vollstaendig schreiben, fsync, os.replace (ersetzt
einen Symlink am Zielnamen, statt ihm zu folgen), fsync des Ordners. Scheitert das Schreiben oder Ersetzen, bleibt keine
Temp-Datei liegen und die urspruengliche Ausnahme geht durch. Der Modus ist Pflicht: kein Aufrufer bekommt je
nach Host 0600 oder 0644. Kern, Laufzeit, Geraetekern und der Gateway-Container schreiben nur hierueber."""
import contextlib
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

PRIVAT = 0o600
LESBAR = 0o644


def schreiben(path: Path, data: bytes, *, modus: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        tmp = path.name + ".tmp"
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=dir_fd)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, modus, dir_fd=dir_fd)
        try:
            try:
                os.fchmod(fd, modus)
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            # keine Temp-Datei zuruecklassen; scheitert auch das Aufraeumen, geht die urspruengliche Ausnahme durch
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dir_fd)
            raise
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def schreiben_json(path: Path, value, *, modus: int) -> None:
    schreiben(path, json.dumps(value, ensure_ascii=False, sort_keys=True).encode(), modus=modus)


def schreiben_text(path: Path, text: str, *, modus: int) -> None:
    schreiben(path, text.encode(), modus=modus)


def lesen_json(path: Path):
    """Inhalt oder None: fehlend oder kaputt (Stromausfall) gilt als leer, nie als Absturz."""
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        logger.warning("%s nicht lesbar (%s), gilt als leer", path.name, type(error).__name__)
        return None
