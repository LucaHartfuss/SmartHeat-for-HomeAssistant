"""Dokumentspeicher des Geraets (Spec 5b 3 und 5.1) und die einzige Stelle fuer atomares Schreiben im Geraetekern
(Audit 4, Befund "dreifach": identity, bootstrap, commands, wish und der Gateway-Host schreiben ueber write_bytes).

Konfiguration und Bedienung liegen je in einer Datei mit Version und Inhalt (ohne schema/version des Vertrags).
Uebernommen wird jede Version, die von der eigenen abweicht, auch eine kleinere (ein aus dem Backup zurueckgespielter
Server, Plan S1); dieselbe Version nur, solange die Bedienung "dirty" ist (lokaler Bedienwunsch seit der letzten
uebernommenen Bedienung, Spec 3.3: der Server schickt eine verlierende Bedienung mit gleicher Version zurueck). Abgelehnt
wird mit einem Grund aus wire.DOKUMENT_ERRORS; die vorige Version bleibt, die Ablehnung steht in dokumente.json.

Mindestversion (Spec 2.1): liegt die eigene Software unter mindest_software, wird die Konfiguration mit update_noetig
abgelehnt (das Geraet regelt mit der alten weiter); eine leere Konfiguration (anlage None, "nicht eingerichtet") wird
immer uebernommen. update_noetig gilt auch nach einem Dokument mit unbekanntem schema und uebersteht Neustarts.
Eine kaputte Datei (Stromausfall) gilt als "keine Version": hello meldet 0, der Server stellt neu zu."""
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypeGuard

from smartheat_core import config_check
from smartheat_device import wire

logger = logging.getLogger(__name__)

KONFIGURATION, BEDIENUNG = wire.DOKUMENTE
DATEIEN = {KONFIGURATION: "konfiguration.json", BEDIENUNG: "bedienung.json"}
ZUSTAND_DATEI = "dokumente.json"
SCHEMA_UNBEKANNT = "schema_unbekannt"
UNGUELTIG = "ungueltig"
UPDATE_NOETIG = "update_noetig"
AUSSERHALB_BEREICH = "ausserhalb_bereich"
SCHEMA_TEXT = "Das Gerät kennt diese Protokollversion nicht. Bitte das Gerät aktualisieren."
FELDER_TEXT = "Der Konfiguration fehlen Felder: {felder}."
UPDATE_TEXT = ("Die Gerätesoftware {eigene} ist älter als die nötige {mindest}. Bitte das Gerät aktualisieren; es regelt "
               "bis dahin mit der bisherigen Einrichtung weiter.")
BEREICH_TEXT = "Die Wunschtemperatur muss zwischen 15 und 25 °C in Schritten von 0,5 K liegen."
MODUS_TEXT = "Diese Betriebsart kennt das Gerät nicht."
QUELLE_TEXT = "Die Bedienung nennt keine gültige Quelle."
_RESERVIERT = ("schema", "version")
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


# --- Dateien ---

def write_bytes(path: Path, data: bytes, *, private: bool = True) -> None:
    """Atomar: temporaere Datei, fsync, rename, fsync des Ordners (wie gateway/files.py, Spec SHG G2 6.1)."""
    mode = 0o600 if private else 0o644
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.fchmod(fd, mode)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def write_json(path: Path, value, *, private: bool = True) -> None:
    write_bytes(path, json.dumps(value, ensure_ascii=False, sort_keys=True).encode(), private=private)


def write_text(path: Path, text: str, *, private: bool = True) -> None:
    write_bytes(path, text.encode(), private=private)


def read_json(path: Path):
    """Inhalt oder None: fehlend oder kaputt (Stromausfall) gilt als leer, nie als Absturz."""
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        logger.warning("%s nicht lesbar (%s), gilt als leer", path.name, type(error).__name__)
        return None


# --- Regeln ---

def im_bedienbereich(value) -> bool:
    """Bedienbereich (Spec 3.2, keine Sicherheitsgrenze): 15-25 °C in 0,5-K-Schritten, wie der Server."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if not wire.RAUM_SOLL_MIN <= value <= wire.RAUM_SOLL_MAX:  # zuerst: exakt fuer grosse int, NaN faellt hier durch
        return False
    steps = value / wire.RAUM_SOLL_STEP
    return abs(steps - round(steps)) < 1e-9


def _version_tuple(text) -> tuple[int, ...] | None:
    match = _VERSION.fullmatch(text) if isinstance(text, str) else None
    return tuple(int(part) for part in match.groups()) if match else None


def unter_mindest(eigene, mindest) -> bool:
    """True, wenn die eigene Version kleiner ist als die Mindestversion; unlesbare Angaben blockieren nie."""
    own, need = _version_tuple(eigene), _version_tuple(mindest)
    return own is not None and need is not None and own < need


def _is_version(value) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _bedienung_pruefen(inhalt: dict) -> config_check.Ablehnung | None:
    if inhalt.get("modus") not in wire.MODI:
        return config_check.Ablehnung(UNGUELTIG, MODUS_TEXT)
    if inhalt.get("quelle") not in wire.QUELLEN:
        return config_check.Ablehnung(UNGUELTIG, QUELLE_TEXT)
    if not im_bedienbereich(inhalt.get("raum_soll")):
        return config_check.Ablehnung(AUSSERHALB_BEREICH, BEREICH_TEXT)
    return None


# --- Speicher ---

@dataclass(frozen=True)
class Dokument:
    art: str
    version: int
    inhalt: dict
    dirty: bool = False


@dataclass(frozen=True)
class Ergebnis:
    """Antwort an den Server (up/result) und Wirkung fuer das Geraet: `uebernommen` = neuer Inhalt, der Host wendet ihn
    an; ok ohne uebernommen = Duplikat (nur das Ergebnis wiederholen)."""
    art: str
    version: int
    ok: bool
    grund: str | None = None
    text: str | None = None
    uebernommen: bool = False

    def payload(self) -> dict:
        body = {"schema": wire.SCHEMA, "dokument": self.art, "version": self.version, "ok": self.ok}
        if not self.ok:
            body["error"] = {"grund": self.grund, "text": self.text or ""}
        return body


class DocumentStore:
    """Schreibt nur im Geraete-Thread; lesen duerfen alle (die Laufzeit liest den Abo-Status)."""

    def __init__(self, directory: Path, *, host: str, software_version: str,
                 treiber_pruefen: config_check.TreiberPruefung | None = None) -> None:
        self._dir = directory
        self._host, self._software_version, self._treiber_pruefen = host, software_version, treiber_pruefen
        self._lock = threading.Lock()
        self._docs: dict[str, Dokument | None] = {art: self._laden(art) for art in wire.DOKUMENTE}
        zustand = read_json(directory / ZUSTAND_DATEI)
        zustand = zustand if isinstance(zustand, dict) else {}
        self._update_noetig = zustand.get("update_noetig") is True
        roh = zustand.get("abgelehnt")
        abgelehnt: dict = roh if isinstance(roh, dict) else {}
        self._abgelehnt: dict[str, tuple[int, str, str]] = {
            art: (value[0], value[1], value[2]) for art, value in abgelehnt.items()
            if art in DATEIEN and isinstance(value, list) and len(value) == 3}

    @property
    def update_noetig(self) -> bool:
        return self._update_noetig

    def _laden(self, art: str) -> Dokument | None:
        raw = read_json(self._dir / DATEIEN[art])
        if not (isinstance(raw, dict) and _is_version(raw.get("version")) and raw["version"] >= 1
                and isinstance(raw.get("inhalt"), dict)):
            return None
        return Dokument(art, raw["version"], raw["inhalt"], raw.get("dirty") is True)

    def _speichern(self, doc: Dokument) -> None:
        write_json(self._dir / DATEIEN[doc.art], {"version": doc.version, "inhalt": doc.inhalt, "dirty": doc.dirty})
        self._docs[doc.art] = doc

    def _zustand_speichern(self) -> None:
        write_json(self._dir / ZUSTAND_DATEI, {"update_noetig": self._update_noetig,
                                                "abgelehnt": {art: list(v) for art, v in self._abgelehnt.items()}})

    def aktuell(self, art: str) -> Dokument | None:
        return self._docs[art]

    def version(self, art: str) -> int:
        doc = self._docs[art]
        return doc.version if doc is not None else 0

    def inhalt(self, art: str) -> dict | None:
        doc = self._docs[art]
        return doc.inhalt if doc is not None else None

    def eingerichtet(self) -> bool:
        inhalt = self.inhalt(KONFIGURATION)
        return inhalt is not None and not config_check.ist_leer(inhalt)

    def ablehnung(self, art: str) -> tuple[int, str, str] | None:
        return self._abgelehnt.get(art)

    def mark_dirty(self) -> None:
        """Lokaler Bedienwunsch (Spec 3.3): die naechste Bedienung wird auch mit gleicher Version uebernommen."""
        with self._lock:
            doc = self._docs[BEDIENUNG]
            if doc is not None and not doc.dirty:
                self._speichern(replace(doc, dirty=True))

    def empfangen(self, art: str, payload: dict) -> Ergebnis | None:
        """down/config bzw. down/operation. None: ohne gueltige Version gibt es nichts zu beantworten."""
        if art not in DATEIEN:
            raise ValueError(f"Dokumentart {art!r} unbekannt")
        version = payload.get("version")
        if not _is_version(version) or version < 1:
            logger.warning("%s ohne gueltige Version verworfen", art)
            return None
        with self._lock:
            if payload.get("schema") not in wire.SCHEMATA:
                if art == KONFIGURATION:
                    self._update_noetig = True
                return self._abgelehnt_mit(art, version, SCHEMA_UNBEKANNT, SCHEMA_TEXT)
            current = self._docs[art]
            if current is not None and current.version == version and not current.dirty:
                return Ergebnis(art, version, True)
            inhalt = {key: value for key, value in payload.items() if key not in _RESERVIERT}
            if art == KONFIGURATION:
                self._update_noetig = unter_mindest(self._software_version, inhalt.get("mindest_software"))
            ablehnung = self._pruefen(art, inhalt, current)
            if ablehnung is not None:
                return self._abgelehnt_mit(art, version, ablehnung.grund, ablehnung.text)
            self._speichern(Dokument(art, version, inhalt))
            self._abgelehnt.pop(art, None)
            self._zustand_speichern()
            return Ergebnis(art, version, True, uebernommen=True)

    def _abgelehnt_mit(self, art: str, version: int, grund: str, text: str) -> Ergebnis:
        self._abgelehnt[art] = (version, grund, text)
        self._zustand_speichern()
        return Ergebnis(art, version, False, grund, text)

    def _pruefen(self, art: str, inhalt: dict, current: Dokument | None) -> config_check.Ablehnung | None:
        if art == BEDIENUNG:
            return _bedienung_pruefen(inhalt)
        fehlen = [field for field in wire.KONFIGURATION_FIELDS if field not in _RESERVIERT and field not in inhalt]
        if fehlen:
            return config_check.Ablehnung(UNGUELTIG, FELDER_TEXT.format(felder=", ".join(fehlen)))
        if config_check.ist_leer(inhalt):
            return None
        if self._update_noetig:
            return config_check.Ablehnung(UPDATE_NOETIG, UPDATE_TEXT.format(eigene=self._software_version,
                                                                            mindest=inhalt.get("mindest_software")))
        vorher = current.inhalt if current is not None else None
        return config_check.pruefen(inhalt, vorher, host=self._host, treiber_pruefen=self._treiber_pruefen)
