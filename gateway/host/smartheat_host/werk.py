"""Die Partition werk: /werk/werk.json (root, 0644, nur lesend eingehaengt) traegt Seriennummer, Hostname und den
scrypt-Hash des Geraetepassworts. `lesen` prueft die Datei streng: ein Feld mehr, ein falscher Typ oder Wert, ein
Symlink oder mehr als 4 KiB machen sie ungueltig (`None`, eine Warnung ohne Inhalt). Der Container sieht `werk` nicht:
`start` (Unit smartheat-werk.service, bei jedem Start vor dem Netz) kopiert die geprueften Werte nach
SHG_ROOT/host/werk.json (0644, im Container nur lesend) und setzt den fluechtigen Hostnamen; ungueltig oder fehlend:
Kopie geloescht, keine Anmeldung moeglich, der Hostname bleibt der des Images. Die Regeln stehen in `_problem` so, dass
der Container sie mit eigenem Code spiegeln kann."""
import base64
import binascii
import json
import logging
import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

from smartheat_core import dateien

logger = logging.getLogger(__name__)

WERK_PFAD = Path("/werk/werk.json")
MAX_BYTES = 4096
FORMAT = "smartheat-werk-1"
HOSTNAME_FRIST_SEKUNDEN = 10
_FELDER = {"format", "seriennummer", "hostname", "passwort", "bespielt", "hardware"}
_PASSWORT = {"verfahren", "n", "r", "p", "salz", "hash"}
_SERIENNUMMER = re.compile(r"[A-Z0-9-]{4,32}", re.ASCII)
_HOSTNAME = re.compile(r"[a-z0-9-]{1,63}", re.ASCII)
_DATUM = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
_SCRYPT = {"n": 131072, "r": 8, "p": 1}


class _DoppelterSchluessel(ValueError):
    pass


def _objekt(paare: list[tuple[str, object]]) -> dict:
    schluessel = [name for name, _ in paare]
    if len(set(schluessel)) != len(schluessel):
        raise _DoppelterSchluessel
    return dict(paare)


def _text(wert) -> bool:
    return type(wert) is str


def _base64(wert, laenge: int) -> bool:
    """Kanonisches Standard-Base64 (mit Auffuellung, ohne Leerraum) von genau `laenge` Byte."""
    if not _text(wert) or not wert.isascii():
        return False
    try:
        roh = base64.b64decode(wert, validate=True)
    except (binascii.Error, ValueError):
        return False
    return len(roh) == laenge and base64.b64encode(roh).decode() == wert


def _datum(wert) -> bool:
    if not _text(wert) or not _DATUM.fullmatch(wert):
        return False
    try:
        date.fromisoformat(wert)
    except ValueError:
        return False
    return True


def _problem(daten) -> str | None:
    """Der Name des ersten verletzten Feldes oder None. Nennt nur Feldnamen, nie Inhalt."""
    if not isinstance(daten, dict) or set(daten) != _FELDER:
        return "Felder"
    if daten["format"] != FORMAT or not _text(daten["format"]):
        return "format"
    if not (_text(daten["seriennummer"]) and _SERIENNUMMER.fullmatch(daten["seriennummer"])):
        return "seriennummer"
    if not (_text(daten["hostname"]) and _HOSTNAME.fullmatch(daten["hostname"])):
        return "hostname"
    passwort = daten["passwort"]
    if not isinstance(passwort, dict) or set(passwort) != _PASSWORT:
        return "passwort"
    if not _text(passwort["verfahren"]) or passwort["verfahren"] != "scrypt":
        return "passwort.verfahren"
    for name, soll in _SCRYPT.items():
        if type(passwort[name]) is not int or passwort[name] != soll:
            return f"passwort.{name}"
    if not _base64(passwort["salz"], 16):
        return "passwort.salz"
    if not _base64(passwort["hash"], 32):
        return "passwort.hash"
    if not _datum(daten["bespielt"]):
        return "bespielt"
    if not (_text(daten["hardware"]) and len(daten["hardware"]) <= 32):
        return "hardware"
    return None


def _roh_lesen(pfad: Path) -> bytes | None:
    """Hoechstens MAX_BYTES einer regulaeren Datei, ohne einem Symlink zu folgen; None (mit Meldung), wenn das nicht geht."""
    try:  # O_NONBLOCK: eine FIFO haelt das Oeffnen nicht fest
        fd = os.open(pfad, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        logger.info("%s fehlt", pfad.name)
        return None
    except OSError as fehler:
        logger.warning("%s ungueltig: nicht lesbar oder ein Symlink (%s)", pfad.name, type(fehler).__name__)
        return None
    try:
        art = os.fstat(fd)
        if not stat.S_ISREG(art.st_mode) or art.st_size > MAX_BYTES:
            logger.warning("%s ungueltig: keine regulaere Datei oder groesser als %d Byte", pfad.name, MAX_BYTES)
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            roh = handle.read(MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(roh) > MAX_BYTES:
        logger.warning("%s ungueltig: groesser als %d Byte", pfad.name, MAX_BYTES)
        return None
    return roh


def lesen(pfad: Path = WERK_PFAD) -> dict | None:
    """Die geprueften Werte von `werk.json` oder None (fehlend oder ungueltig; Warnung ohne Inhalt)."""
    roh = _roh_lesen(pfad)
    if roh is None:
        return None
    try:
        daten = json.loads(roh.decode(), object_pairs_hook=_objekt, parse_constant=_ablehnen)
    except (UnicodeDecodeError, ValueError):
        logger.warning("%s ungueltig: kein JSON-Objekt (oder ein Schluessel doppelt)", pfad.name)
        return None
    problem = _problem(daten)
    if problem is not None:
        logger.warning("%s ungueltig: Feld %s", pfad.name, problem)
        return None
    return daten


def _ablehnen(name: str):
    raise ValueError(name)


def _kopie_loeschen(ziel: Path) -> bool:
    try:
        ziel.unlink()
    except FileNotFoundError:
        pass
    except OSError as fehler:
        logger.error("%s nicht zu loeschen (%s)", ziel, type(fehler).__name__)
        return False
    return True


def main(argv: list[str] | None = None, *, pfad: Path = WERK_PFAD,
         run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args != ["start"]:
        print("Aufruf: python3 -m smartheat_host.werk start", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO)
    ziel = Path(os.environ.get("SHG_ROOT", "/var/lib/smartheat")) / "host" / "werk.json"
    daten = lesen(pfad)
    if daten is None:
        return 0 if _kopie_loeschen(ziel) else 1
    dateien.schreiben_json(ziel, daten, modus=dateien.LESBAR)
    try:
        ergebnis = run(["hostnamectl", "--transient", "hostname", daten["hostname"]], capture_output=True, text=True,
                       check=False, timeout=HOSTNAME_FRIST_SEKUNDEN)
    except (OSError, subprocess.TimeoutExpired) as fehler:
        logger.error("Hostname nicht gesetzt (%s)", type(fehler).__name__)
        return 1
    if ergebnis.returncode != 0:
        logger.error("Hostname nicht gesetzt (hostnamectl Exit %d)", ergebnis.returncode)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - systemd-Einstieg
    sys.exit(main())
