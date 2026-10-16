"""Dokumentspeicher des Geraets (Spec 5b 3 und 5.1); geschrieben wird ueber smartheat_core.dateien.

Konfiguration und Bedienung liegen je in einer Datei mit Version und Inhalt (ohne schema/version des Vertrags).
Uebernommen wird jede Version, die von der eigenen abweicht, auch eine kleinere (ein aus dem Backup zurueckgespielter
Server, Plan S1); dieselbe Version nur, solange die Bedienung "dirty" ist (lokaler Bedienwunsch seit der letzten
uebernommenen Bedienung, Spec 3.3: der Server schickt eine verlierende Bedienung mit gleicher Version zurueck). Abgelehnt
wird mit einem Grund aus wire.DOKUMENT_ERRORS und details; die vorige Version bleibt, die Ablehnung steht in dokumente.json.

Mindestversion (Spec 2.1): liegt die eigene Software unter mindest_software, wird die Konfiguration mit update_noetig
abgelehnt (das Geraet regelt mit der alten weiter); eine leere Konfiguration (anlage None, "nicht eingerichtet") wird
immer uebernommen. update_noetig gilt auch nach einem Dokument mit unbekanntem schema und uebersteht Neustarts. Der
Server schickt eine abgelehnte Version nicht erneut; gespeichert wird daher der Anlass (die mindest_software der
abgelehnten Konfiguration bzw. die eigene Software beim unbekannten schema), und update_noetig wird gegen die laufende
Software ausgewertet: nach einem Update endet es von selbst. Nur eine vollstaendige Konfiguration (alle Felder) setzt den
Anlass neu; eine wegen fehlender Felder abgelehnte laesst ihn stehen.
Wirft der Treiber-Rueckruf config_check.NichtPruefbar (HA aus), laeuft empfangen durch: nichts wird gespeichert oder
abgelehnt, das Geraet haelt das Dokument zurueck und prueft spaeter erneut (device.py).
Eine kaputte Datei (Stromausfall) gilt als "keine Version": hello meldet 0, der Server stellt neu zu.

Rastung (Spec Stufe 3 S3 Abschnitt 3, A5-06): Hebelsatz x Verteilsystem der ersten eingerichteten Konfiguration stehen in
sicherheitssatz.json; kein Dokument loest sie, auch keine leere Konfiguration. Sie wird vor dem Speichern der Konfiguration
geschrieben. Fehlt die Datei bei gespeicherter eingerichteter Konfiguration (Stand vor dieser Version), rastet der Start.
Eine kaputte oder manipulierte Datei (auch ein Symlink) lehnt jede eingerichtete Konfiguration ab (rastung.lesen)."""
import logging
import re
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypeGuard

from smartheat_core import config_check, dateien, rastung, wallclock
from smartheat_core.rastung import Rastung
from smartheat_device import fehler, nachrichten, wire

logger = logging.getLogger(__name__)

KONFIGURATION, BEDIENUNG = wire.DOKUMENTE
DATEIEN = {KONFIGURATION: "konfiguration.json", BEDIENUNG: "bedienung.json"}
ZUSTAND_DATEI = "dokumente.json"
SCHEMA_UNBEKANNT = "schema_unbekannt"
UNGUELTIG = "ungueltig"
UPDATE_NOETIG = "update_noetig"
AUSSERHALB_BEREICH = "ausserhalb_bereich"
_RESERVIERT = ("schema", "version")
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


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


def _anlass(raw) -> tuple[str, str] | None:
    """Gespeicherter Anlass fuer update_noetig: [grund, version]; alles andere gilt als kein Anlass."""
    if (isinstance(raw, list) and len(raw) == 2 and raw[0] in (UPDATE_NOETIG, SCHEMA_UNBEKANNT)
            and isinstance(raw[1], str)):
        return raw[0], raw[1]
    return None


def _is_version(value) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _bedienung_pruefen(inhalt: dict) -> config_check.Ablehnung | None:
    if inhalt.get("modus") not in wire.MODI:
        return config_check.Ablehnung(UNGUELTIG, fehler.details("modus", "unbekannt"))
    if inhalt.get("quelle") not in wire.QUELLEN:
        return config_check.Ablehnung(UNGUELTIG, fehler.details("quelle", "unbekannt"))
    if not im_bedienbereich(inhalt.get("raum_soll")):
        return config_check.Ablehnung(AUSSERHALB_BEREICH, fehler.details(
            "raum_soll", "bereich", min=wire.RAUM_SOLL_MIN, max=wire.RAUM_SOLL_MAX))
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
    an; ok ohne uebernommen = Duplikat (nur das Ergebnis wiederholen). Abgelehnt: Grund und details, kein Text."""
    art: str
    version: int
    ok: bool
    grund: str | None = None
    details: dict | None = None
    uebernommen: bool = False

    def payload(self) -> dict:
        error = None
        if not self.ok:
            assert self.grund is not None
            error = fehler.body(self.grund, self.details)
        return nachrichten.ergebnis_dokument(self.art, self.version, self.ok, error)


class DocumentStore:
    """Schreibt nur im Geraete-Thread; lesen duerfen alle (die Laufzeit liest den Abo-Status)."""

    def __init__(self, directory: Path, *, host: str, software_version: str,
                 treiber_pruefen: config_check.TreiberPruefung | None = None) -> None:
        self._dir = directory
        self._host, self._software_version, self._treiber_pruefen = host, software_version, treiber_pruefen
        self._lock = threading.Lock()
        self._docs: dict[str, Dokument | None] = {art: self._laden(art) for art in wire.DOKUMENTE}
        self._rastung_pfad = directory / rastung.DATEI
        self._rastung = self._rastung_laden()
        zustand = dateien.lesen_json(directory / ZUSTAND_DATEI)
        zustand = zustand if isinstance(zustand, dict) else {}
        self._update_anlass = _anlass(zustand.get("update_anlass"))
        roh = zustand.get("abgelehnt")
        abgelehnt: dict = roh if isinstance(roh, dict) else {}
        # eine Ablehnung von vor 5c traegt an Stelle 2 einen Text: sie laedt ohne details
        self._abgelehnt: dict[str, tuple[int, str, dict | None]] = {
            art: (value[0], value[1], value[2] if isinstance(value[2], dict) else None)
            for art, value in abgelehnt.items() if art in DATEIEN and isinstance(value, list) and len(value) == 3}

    @property
    def update_noetig(self) -> bool:
        anlass = self._update_anlass
        if anlass is None:
            return False
        if anlass[0] == SCHEMA_UNBEKANNT:
            return anlass[1] == self._software_version
        return unter_mindest(self._software_version, anlass[1])

    @property
    def rastung(self) -> Rastung | None:
        return self._rastung

    def _rastung_laden(self) -> Rastung | None:
        """Spec 3.3: fehlt die Datei und ist die gespeicherte Konfiguration eingerichtet, gilt deren Paar (Regel "erste
        eingerichtete Konfiguration", z. B. client1 beim ersten Start mit dieser Version). Eine vorhandene, auch
        kaputte Datei wird nie ueberschrieben."""
        gelesen = rastung.lesen(self._rastung_pfad)
        inhalt = self.inhalt(KONFIGURATION)
        if gelesen is None and inhalt is not None and not config_check.ist_leer(inhalt):
            return self._rasten(inhalt)
        return gelesen

    def _rasten(self, inhalt: dict) -> Rastung | None:
        hebelsatz, verteilsystem = inhalt.get("hebelsatz"), inhalt.get("verteilsystem")
        if not (isinstance(hebelsatz, str) and isinstance(verteilsystem, str)):
            return None
        neu = Rastung(hebelsatz, verteilsystem)
        rastung.schreiben(self._rastung_pfad, neu, wallclock.now().isoformat(timespec="seconds"))
        logger.info("Sicherheitssatz eingerastet")
        return neu

    def _laden(self, art: str) -> Dokument | None:
        raw = dateien.lesen_json(self._dir / DATEIEN[art])
        if not (isinstance(raw, dict) and _is_version(raw.get("version")) and raw["version"] >= 1
                and isinstance(raw.get("inhalt"), dict)):
            return None
        return Dokument(art, raw["version"], raw["inhalt"], raw.get("dirty") is True)

    def _speichern(self, doc: Dokument) -> None:
        dateien.schreiben_json(self._dir / DATEIEN[doc.art],
                               {"version": doc.version, "inhalt": doc.inhalt, "dirty": doc.dirty}, modus=dateien.PRIVAT)
        self._docs[doc.art] = doc

    def _zustand_speichern(self) -> None:
        anlass = list(self._update_anlass) if self._update_anlass is not None else None
        dateien.schreiben_json(self._dir / ZUSTAND_DATEI,
                               {"update_anlass": anlass,
                                "abgelehnt": {art: list(v) for art, v in self._abgelehnt.items()}}, modus=dateien.PRIVAT)

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

    def ablehnung(self, art: str) -> tuple[int, str, dict | None] | None:
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
                    self._update_anlass = (SCHEMA_UNBEKANNT, self._software_version)
                return self._abgelehnt_mit(art, version, SCHEMA_UNBEKANNT, None)
            current = self._docs[art]
            if current is not None and current.version == version and not current.dirty:
                return Ergebnis(art, version, True)
            inhalt = {key: value for key, value in payload.items() if key not in _RESERVIERT}
            ablehnung = self._pruefen(art, inhalt)
            if ablehnung is not None:
                return self._abgelehnt_mit(art, version, ablehnung.grund, ablehnung.details)
            if art == KONFIGURATION and self._rastung is None and not config_check.ist_leer(inhalt):
                self._rastung = self._rasten(inhalt)  # vor dem Dokument (Spec 3.2): ein Absturz dazwischen schadet nicht
            self._speichern(Dokument(art, version, inhalt))
            self._abgelehnt.pop(art, None)
            self._zustand_speichern()
            return Ergebnis(art, version, True, uebernommen=True)

    def _abgelehnt_mit(self, art: str, version: int, grund: str, details: dict | None) -> Ergebnis:
        if not fehler.gueltig(details):
            logger.error("%s abgelehnt (%s): details ohne die Form des Vertrags verworfen", art, grund)
            details = None
        self._abgelehnt[art] = (version, grund, details)
        self._zustand_speichern()
        return Ergebnis(art, version, False, grund, details)

    def _pruefen(self, art: str, inhalt: dict) -> config_check.Ablehnung | None:
        if art == BEDIENUNG:
            return _bedienung_pruefen(inhalt)
        fehlen = [field for field in wire.KONFIGURATION_FIELDS if field not in _RESERVIERT and field not in inhalt]
        if fehlen:
            return config_check.Ablehnung(UNGUELTIG, fehler.details(fehlen[0], "fehlt", anzahl=len(fehlen)))
        mindest = inhalt.get("mindest_software")  # erst eine vollstaendige Konfiguration setzt den Anlass neu
        self._update_anlass = (UPDATE_NOETIG, mindest) if unter_mindest(self._software_version, mindest) else None
        if config_check.ist_leer(inhalt):
            return None
        if self._update_anlass is not None:
            return config_check.Ablehnung(UPDATE_NOETIG)
        return config_check.pruefen(inhalt, self._rastung, host=self._host, treiber_pruefen=self._treiber_pruefen)
