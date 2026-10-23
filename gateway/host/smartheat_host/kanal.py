"""Lokaler Kanal des Updaters: ersetzt die signierten HTTPS-Anfragen an die Geraete-API. Die Laufzeit (Geraetekern) schreibt die Soll-Software aus dem Konfigurationsdokument nach
data/software_soll.json; der Updater schreibt seine Ergebnisse nach data/software_meldungen.json (fortlaufende seq, die
letzten MELDUNGEN_MAX), die Laufzeit sendet sie als up/result (dokument "software") und quittiert die zuletzt gesendete
seq in data/software_quittung.json. Je Datei genau ein Schreiber.

Erreichbarkeit fuer die Gesundheitspruefung: TCP-Verbindung zum Broker aus data/device/endpoint.json (Netz und DNS;
unabhaengig vom neuen Bundle). Standardbibliothek; smartheat_device ist auf dem Host nicht installiert, die Dateinamen
haelt test_kanal gleich."""
import contextlib
import json
import os
import socket
import stat
from collections.abc import Callable
from pathlib import Path

from smartheat_core import dateien

from smartheat_host.lesen import read_bytes, read_json

SOLL_FILE = "software_soll.json"
MELDUNGEN_FILE = "software_meldungen.json"
QUITTUNG_FILE = "software_quittung.json"
ENDPOINT_FILE = "endpoint.json"  # smartheat_device.bootstrap.ENDPOINT_FILE, unter data/device/
MELDUNGEN_MAX = 20


class KanalFehler(Exception):
    """Vorlaeufig: Soll-Software noch nicht da oder unlesbar, Datei nicht schreibbar; naechster Durchlauf."""


def _seq(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


class LokalerKanal:
    def __init__(self, data_dir: Path, *, connect: Callable = socket.create_connection, timeout: float = 5.0) -> None:
        self._data, self._connect, self._timeout = data_dir, connect, timeout

    def desired(self) -> dict:
        try:
            raw = json.loads(read_bytes(self._data / SOLL_FILE))  # begrenzt, ohne Symlink, nie an einer FIFO haengend
        except FileNotFoundError:
            raise KanalFehler("Soll-Software noch nicht bekannt (Laufzeit hat keine Konfiguration geschrieben)") from None
        except (OSError, ValueError) as error:
            raise KanalFehler(f"Soll-Software unlesbar ({type(error).__name__})") from None
        if raw is None:
            return {}  # Konfiguration ohne Software-Angebot
        if not isinstance(raw, dict):
            raise KanalFehler("Soll-Software ist kein Objekt")
        return raw

    def update_result(self, version: str, result: str, reason: str) -> None:
        raw = read_json(self._data / MELDUNGEN_FILE)
        meldungen = raw.get("meldungen") if isinstance(raw, dict) else None
        meldungen = [m for m in meldungen if isinstance(m, dict)] if isinstance(meldungen, list) else []
        quittung = read_json(self._data / QUITTUNG_FILE)
        letzte = max([_seq(m.get("seq")) for m in meldungen]
                     + [_seq(quittung.get("seq")) if isinstance(quittung, dict) else 0])
        meldungen.append({"seq": letzte + 1, "version": version, "result": result, "reason": reason})
        try:
            # LESBAR (0644): die Laufzeit im Container liest die Meldungen
            dateien.schreiben_json(self._data / MELDUNGEN_FILE, {"meldungen": meldungen[-MELDUNGEN_MAX:]},
                                   modus=dateien.LESBAR)
        except OSError as error:
            raise KanalFehler(f"Meldung nicht schreibbar ({type(error).__name__})") from None

    def erreichbar(self) -> bool:
        try:
            if not stat.S_ISDIR(os.lstat(self._data / "device").st_mode):  # lstat: ein Symlink ist kein Ordner
                return False
        except OSError:
            return False
        raw = read_json(self._data / "device" / ENDPOINT_FILE)
        if not (isinstance(raw, dict) and isinstance(raw.get("host"), str) and isinstance(raw.get("port"), int)):
            return False
        try:
            sock = self._connect((raw["host"], raw["port"]), self._timeout)
        except OSError:  # auch socket.gaierror (DNS) und TimeoutError
            return False
        with contextlib.suppress(OSError):
            sock.close()
        return True
