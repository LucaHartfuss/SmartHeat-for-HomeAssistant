"""Anmeldung an der LAN-Seite mit dem Etikett-Passwort (SG-05, S-13, GW 8): Pruefung gegen den scrypt-Hash aus
`werk.json`, globale Drossel fuer das Geraet und Sitzungen nur im Speicher.

`werk.json` liegt auf der Partition `werk`; der Container sieht sie nicht. `smartheat-werk.service` kopiert die vom Host
geprueften Werte nach SHG_ROOT/host/werk.json (im Container `SHG_HOST_DIR/werk.json`, nur lesend). `werk_lesen` prueft
sie mit eigenem Code noch einmal (die Paketgrenze des Host-Pakets verbietet den gemeinsamen Import) nach denselben
Regeln wie `smartheat_host.werk`; ein gemeinsamer Fallliste-Test (host/tests/test_werk.py) haelt beide gleich. Ungueltig
oder fehlend: keine Anmeldung moeglich.

Drossel: ab dem 5. Fehlversuch in Folge Sperre von 60 s, jede weitere Sperre doppelt, hoechstens 3600 s; ein Erfolg
setzt zurueck; waehrend der Sperre wird nicht geprueft. Ein einziger Lock umfasst Sperrpruefung, scrypt und Zaehlung und
wird nur mit `acquire(blocking=False)` genommen: gleichzeitige Versuche werden abgewiesen (`belegt`), ohne zu pruefen
und ohne zu zaehlen. So rechnet nie mehr als ein scrypt (128 MiB, rund 0,3 bis 1 s auf dem Pi) zugleich; viele
parallele Anmeldungen aus dem LAN koennen weder den Speicher fuellen noch die Regelung im selben Prozess aushungern
(Gefaehrdungsanalyse, Regelung faellt aus, Ursache aus dem LAN)."""
import base64
import binascii
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import stat
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

SCRYPT_N = 131072
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 268435456  # 256 MiB; scrypt braucht bei n=131072, r=8 rund 128 MiB
SALZ_BYTES = 16
HASH_BYTES = 32

FREI_FEHLVERSUCHE = 5  # ab dem 5. Fehlversuch in Folge Sperre
SPERRE_ERSTE = 60.0
SPERRE_MAX = 3600.0
SITZUNG_SEKUNDEN = 3600.0
SITZUNG_MAX = 16

WERK_DATEI = "werk.json"
WERK_MAX_BYTES = 4096
_FORMAT = "smartheat-werk-1"
_FELDER = {"format", "seriennummer", "hostname", "passwort", "bespielt", "hardware"}
_PASSWORT = {"verfahren", "n", "r", "p", "salz", "hash"}
_SERIENNUMMER = re.compile(r"[A-Z0-9-]{4,32}", re.ASCII)
_HOSTNAME = re.compile(r"[a-z0-9-]{1,63}", re.ASCII)
_DATUM = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)


def _b64(roh: bytes) -> str:
    return base64.b64encode(roh).decode()


def _hash(passwort: str, salz: bytes) -> bytes:
    return hashlib.scrypt(passwort.encode(), salt=salz, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=SCRYPT_MAXMEM,
                          dklen=HASH_BYTES)


def passwort_eintrag(passwort: str, salz: bytes) -> dict:
    """Das Feld `passwort` von werk.json fuer `passwort` und `salz` (16 Byte); fuer die Bespiel-Station und Tests."""
    if len(salz) != SALZ_BYTES:
        raise ValueError("Salz muss 16 Byte lang sein")
    return {"verfahren": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P, "salz": _b64(salz),
            "hash": _b64(_hash(passwort, salz))}


def pruefe(eintrag: dict, passwort: str) -> bool:
    """Passt `passwort` zum Feld `passwort` von werk.json? Vergleich in konstanter Zeit; die scrypt-Parameter sind fest
    (nicht aus dem Eintrag), damit ein Eintrag den Speicherbedarf nicht bestimmt."""
    erwartet = base64.b64decode(eintrag["hash"], validate=True)
    return hmac.compare_digest(_hash(passwort, base64.b64decode(eintrag["salz"], validate=True)), erwartet)


# --- werk.json (Spiegel von smartheat_host.werk) ---------------------------------------------------------------------


class _DoppelterSchluessel(ValueError):
    pass


def _objekt(paare: list[tuple[str, object]]) -> dict:
    schluessel = [name for name, _ in paare]
    if len(set(schluessel)) != len(schluessel):
        raise _DoppelterSchluessel
    return dict(paare)


def _ablehnen(name: str):
    raise ValueError(name)


def _text(wert) -> bool:
    return type(wert) is str


def _base64(wert, laenge: int) -> bool:
    if not _text(wert) or not wert.isascii():
        return False
    try:
        roh = base64.b64decode(wert, validate=True)
    except (binascii.Error, ValueError):
        return False
    return len(roh) == laenge and _b64(roh) == wert


def _datum(wert) -> bool:
    if not _text(wert) or not _DATUM.fullmatch(wert):
        return False
    try:
        date.fromisoformat(wert)
    except ValueError:
        return False
    return True


def _problem(daten) -> str | None:
    """Der Name des ersten verletzten Feldes oder None; nennt nie Inhalt."""
    if not isinstance(daten, dict) or set(daten) != _FELDER:
        return "Felder"
    if not _text(daten["format"]) or daten["format"] != _FORMAT:
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
    for name, soll in (("n", SCRYPT_N), ("r", SCRYPT_R), ("p", SCRYPT_P)):
        if type(passwort[name]) is not int or passwort[name] != soll:
            return f"passwort.{name}"
    if not _base64(passwort["salz"], SALZ_BYTES):
        return "passwort.salz"
    if not _base64(passwort["hash"], HASH_BYTES):
        return "passwort.hash"
    if not _datum(daten["bespielt"]):
        return "bespielt"
    if not (_text(daten["hardware"]) and len(daten["hardware"]) <= 32):
        return "hardware"
    return None


def _roh_lesen(pfad: Path) -> bytes | None:
    try:  # O_NONBLOCK: eine FIFO haelt das Oeffnen nicht fest
        fd = os.open(pfad, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as fehler:
        logger.warning("%s ungueltig: nicht lesbar oder ein Symlink (%s)", pfad.name, type(fehler).__name__)
        return None
    try:
        art = os.fstat(fd)
        if not stat.S_ISREG(art.st_mode) or art.st_size > WERK_MAX_BYTES:
            logger.warning("%s ungueltig: keine regulaere Datei oder groesser als %d Byte", pfad.name, WERK_MAX_BYTES)
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            roh = handle.read(WERK_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(roh) > WERK_MAX_BYTES:
        logger.warning("%s ungueltig: groesser als %d Byte", pfad.name, WERK_MAX_BYTES)
        return None
    return roh


def werk_lesen(pfad: Path) -> dict | None:
    """Die geprueften Werte der Kopie von werk.json oder None (fehlend oder ungueltig; Warnung ohne Inhalt)."""
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


# --- Drossel und Sitzungen -------------------------------------------------------------------------------------------


class Pruefstelle:
    """Die Passwortpruefung des Geraets mit globaler Drossel. `eintrag_lesen` liefert das Feld `passwort` von werk.json
    oder None (kein gueltiges Werk), `jetzt` die monotone Uhr in Sekunden."""

    def __init__(self, eintrag_lesen: Callable[[], dict | None], jetzt: Callable[[], float] = time.monotonic) -> None:
        self._eintrag_lesen = eintrag_lesen
        self._jetzt = jetzt
        self._lock = threading.Lock()
        self._fehler = 0
        self._sperre = 0.0  # Dauer der letzten Sperre
        self._gesperrt_bis = 0.0

    def anmelden(self, passwort: str) -> str:
        """`ok`, `falsch`, `gesperrt` (Sperre laeuft, nicht geprueft), `belegt` (eine andere Pruefung laeuft, nicht
        geprueft, nicht gezaehlt) oder `kein_werk`."""
        eintrag = self._eintrag_lesen()
        if eintrag is None:
            return "kein_werk"
        if not self._lock.acquire(blocking=False):
            return "belegt"
        try:
            jetzt = self._jetzt()
            if jetzt < self._gesperrt_bis:
                return "gesperrt"
            if pruefe(eintrag, passwort):
                self._fehler = 0
                self._sperre = 0.0
                return "ok"
            self._fehler += 1
            if self._fehler >= FREI_FEHLVERSUCHE:
                self._sperre = SPERRE_ERSTE if self._sperre == 0.0 else min(self._sperre * 2, SPERRE_MAX)
                self._gesperrt_bis = jetzt + self._sperre
                logger.warning("Anmeldung gesperrt fuer %d s nach %d Fehlversuchen", self._sperre, self._fehler)
            return "falsch"
        finally:
            self._lock.release()

    def sperre_rest(self) -> int:
        """Ganze Sekunden bis zum Ende der Sperre, mindestens 1 (fuer Retry-After; auch bei `belegt`)."""
        return max(1, math.ceil(self._gesperrt_bis - self._jetzt()))


class Sitzungen:
    """Angemeldete Browser: Tokens nur im Speicher, 1 h gueltig, hoechstens 16 (die aelteste faellt)."""

    def __init__(self, jetzt: Callable[[], float] = time.monotonic) -> None:
        self._jetzt = jetzt
        self._lock = threading.Lock()
        self._ablauf: OrderedDict[str, float] = OrderedDict()

    def neu(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._ablauf[token] = self._jetzt() + SITZUNG_SEKUNDEN
            while len(self._ablauf) > SITZUNG_MAX:
                self._ablauf.popitem(last=False)
        return token

    def gueltig(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            jetzt = self._jetzt()
            for abgelaufen in [name for name, ablauf in self._ablauf.items() if jetzt >= ablauf]:
                del self._ablauf[abgelaufen]
            treffer = False
            for name in self._ablauf:  # ueber alle, in konstanter Zeit je Eintrag
                treffer |= hmac.compare_digest(name.encode(), token.encode())
            return treffer


class Zugang:
    """Was die LAN-Seite braucht: das Werk (Seriennummer, Hostname), die Pruefstelle und die Sitzungen."""

    def __init__(self, werk_pfad: Path, jetzt: Callable[[], float] = time.monotonic) -> None:
        self._werk_pfad = werk_pfad
        self.pruefstelle = Pruefstelle(self._eintrag, jetzt)
        self.sitzungen = Sitzungen(jetzt)

    def werk(self) -> dict | None:
        return werk_lesen(self._werk_pfad)

    def _eintrag(self) -> dict | None:
        werk = self.werk()
        return werk["passwort"] if werk is not None else None
