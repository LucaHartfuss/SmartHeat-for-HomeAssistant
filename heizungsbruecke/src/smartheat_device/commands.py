"""Befehle (Spec 5b 3.4): Register, in das der Host seine Befehle eintraegt, und Einmal-Ausfuehrung (Muster des
Gateway-Agenten bis 0.5.0). Ein Befehl wird hoechstens einmal ausgefuehrt; sein Ergebnis steht vor dem Senden in
<daten>/device/befehle.json (Liste [command_id, ok, result, error], letzte DONE_KEEP, aelteste zuerst) und geht bei
einem Duplikat (erneute Zustellung nach hello, auch ueber einen Neustart) erneut hoch; der Server behaelt das erste.
Ueber den Ablauf entscheidet nur der Server (expires_at wird nicht gegen die eigene Uhr geprueft). Jeder gemeldete
Grund steht in der Liste des Befehls (wire.command_errors), sonst geht `intern` hoch und die Fehlerart ins Log.

Hoechstens einmal gilt auch ueber einen Neustart: bevor ein Handler laeuft, steht in derselben Liste ein Marker
"gestartet" ([command_id, null, null, null]). Kommt eine so markierte command_id nach einem Neustart erneut (weder
wartend noch fertig; der Handler lief schon, sein Ergebnis ist unbekannt), laeuft der Handler nicht noch einmal, der
Befehl endet mit `intern` (UNTERBROCHEN_TEXT) und dieses Ergebnis ersetzt den Marker.

Ein Handler liefert Done, Failed oder Waiting; wartende Befehle prueft pruefen() ohne zu blockieren. Done kann eine
Inventur tragen: sie geht als up/inventory vor dem Ergebnis (Spec 3.4); ohne Verbindung wartet der Befehl, bis sie
gesendet ist. Eingebaut, weil jedes Geraet ihn koennen muss: hinweis (Hinweise)."""
import logging
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from smartheat_device import wire
from smartheat_device.documents import read_json, write_json

logger = logging.getLogger(__name__)

STORE_FILE = "befehle.json"
HINWEISE_FILE = "hinweise.json"
DONE_KEEP = 500
COMMAND_ID_MAX_CHARS = 64
TIMEOUT_GRUND = "keine_bestaetigung"
TIMEOUT_TEXT = "Das Gerät hat die Änderung nicht rechtzeitig bestätigt."
INTERN_TEXT = "Interner Fehler im Gerät."
UNKNOWN_TEXT = "Unbekannter Befehl."
INVENTUR_TEXT = "Die Inventur ist zu groß für eine Übertragung."
UNTERBROCHEN_TEXT = "Der Befehl wurde durch einen Neustart des Geräts unterbrochen."
HINWEIS_KEY_MAX_CHARS = 64
HINWEIS_TEXT_MAX_CHARS = 1000


@dataclass(frozen=True)
class Done:
    result: dict
    inventur: dict | None = None  # Spec 3.4: geht als up/inventory vor dem Ergebnis


@dataclass(frozen=True)
class Failed:
    grund: str
    text: str


@dataclass(frozen=True)
class Waiting:
    check: Callable[[], "Done | Failed | None"]
    deadline: float  # monotone Uhr


Outcome = Done | Failed | Waiting
Handler = Callable[[dict], Outcome]
Entry = tuple[bool | None, dict | None, dict | None]  # (ok, result, error) wie in up/result; ok None = gestartet


class InvalidPayload(ValueError):
    """Nutzlast passt nicht zum Befehl; der Text geht als ungueltige_nutzlast an den Server."""


def result_payload(command_id: str, ok: bool, result: dict | None, error: dict | None) -> dict:
    return {"schema": wire.SCHEMA, "command_id": command_id, "ok": ok, "result": result if ok else None,
            "error": None if ok else error}


def _load_done(path: Path) -> dict[str, Entry]:
    entries = read_json(path)
    done: dict[str, Entry] = {}
    if not isinstance(entries, list):
        return done
    for entry in entries:
        if (isinstance(entry, list) and len(entry) == 4 and isinstance(entry[0], str) and isinstance(entry[1], bool | None)
                and isinstance(entry[2], dict | None) and isinstance(entry[3], dict | None)):
            done[entry[0]] = (entry[1], entry[2], entry[3])
    return done


def _frame(error: Exception) -> str:
    frames = traceback.extract_tb(error.__traceback__)
    if not frames:
        return ""
    return f" in {Path(frames[-1].filename).name}:{frames[-1].lineno} {frames[-1].name}"


class CommandRegister:
    """Register und Einmal-Ausfuehrung; alle Methoden laufen im Geraete-Thread."""

    def __init__(self, device_dir: Path, send_result: Callable[[dict], bool], *,
                 send_inventory: Callable[[str, dict], bool] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 uebersetzen: Callable[[Exception], Failed | None] = lambda error: None) -> None:
        self._path = device_dir / STORE_FILE
        self._send_result, self._send_inventory = send_result, send_inventory
        self._clock, self._uebersetzen = clock, uebersetzen
        self._handlers: dict[str, Handler] = {}
        self._pending: dict[str, tuple[str, Waiting]] = {}
        self._done = _load_done(self._path)

    def register(self, kind: str, handler: Handler) -> None:
        if kind not in wire.COMMANDS:
            raise ValueError(f"Befehlsart {kind!r} steht nicht im Vertrag")
        self._handlers[kind] = handler

    @property
    def kinds(self) -> frozenset[str]:
        return frozenset(self._handlers)

    def empfangen(self, message: dict) -> None:
        """down/command: einmal ausfuehren; ein Duplikat wiederholt nur das gespeicherte Ergebnis."""
        command_id = message.get("command_id")
        if not isinstance(command_id, str) or not 0 < len(command_id) <= COMMAND_ID_MAX_CHARS:
            logger.warning("Befehl ohne gueltige command_id verworfen")
            return
        if command_id in self._pending:
            return
        if command_id in self._done:
            ok, result, error = self._done[command_id]
            if ok is None:  # gestartet, Ergebnis unbekannt (Neustart): nicht noch einmal ausfuehren
                self._finish_or_wait(command_id, "", Failed("intern", UNTERBROCHEN_TEXT))
            else:
                self._send_result(result_payload(command_id, ok, result, error))
            return
        kind = message.get("kind")
        kind = kind if isinstance(kind, str) else ""
        self._finish_or_wait(command_id, kind, self._execute(command_id, kind, message.get("payload")))

    def pruefen(self) -> None:
        """Wartende Befehle (Takt des Geraets): fertig, gescheitert oder Frist abgelaufen."""
        for command_id, (kind, waiting) in list(self._pending.items()):
            outcome = self._guarded(kind, waiting.check)
            if outcome is None and self._clock() > waiting.deadline:
                outcome = self._timed_out(kind)
            if outcome is not None:
                del self._pending[command_id]
                self._finish_or_wait(command_id, kind, outcome)

    # --- intern ---

    def _execute(self, command_id: str, kind: str, payload) -> Outcome:
        handler = self._handlers.get(kind)
        if handler is None or not isinstance(payload, dict):
            return Failed("ungueltige_nutzlast", UNKNOWN_TEXT)
        self._store(command_id, (None, None, None))  # Marker "gestartet" vor dem Handler
        outcome = self._guarded(kind, lambda: handler(payload))
        return outcome if outcome is not None else Failed("intern", INTERN_TEXT)

    def _guarded(self, kind: str, call):
        """Fuehrt call aus und uebersetzt Ausnahmen in Failed mit einem Grund aus der Liste des Befehls."""
        try:
            outcome = call()
        except InvalidPayload as error:
            outcome = Failed("ungueltige_nutzlast", str(error))
        except Exception as error:
            outcome = self._uebersetzen(error)
            if outcome is None:
                # nur Fehlerart und innerster Frame: Meldung und Quelltext koennen Nutzlast-Teile tragen (Regel 6)
                logger.error("Befehl %s fehlgeschlagen (%s)%s", kind, type(error).__name__, _frame(error))
                return Failed("intern", INTERN_TEXT)
        if isinstance(outcome, Failed) and kind in wire.COMMANDS and outcome.grund not in wire.command_errors(kind):
            logger.error("Befehl %s: Grund %s steht nicht in der Vertragsliste", kind, outcome.grund)
            return Failed("intern", INTERN_TEXT)
        return outcome

    def _timed_out(self, kind: str) -> Failed:
        if kind in wire.COMMANDS and TIMEOUT_GRUND in wire.command_errors(kind):
            return Failed(TIMEOUT_GRUND, TIMEOUT_TEXT)
        logger.error("Befehl %s: Zeitablauf steht nicht in der Vertragsliste", kind)
        return Failed("intern", INTERN_TEXT)

    def _finish_or_wait(self, command_id: str, kind: str, outcome: Outcome) -> None:
        if isinstance(outcome, Waiting):
            self._pending[command_id] = (kind, outcome)
            return
        if isinstance(outcome, Done) and outcome.inventur is not None:
            sent = self._inventur_senden(command_id, kind, outcome)
            if sent is None:
                return  # ohne Verbindung: pruefen() versucht es erneut
            outcome = sent
        if isinstance(outcome, Done):
            ok, result, error = True, outcome.result, None
        else:
            ok, result, error = False, None, {"grund": outcome.grund, "text": outcome.text}
        self._store(command_id, (ok, result, error))  # zuerst dauerhaft, dann melden
        self._send_result(result_payload(command_id, ok, result, error))

    def _inventur_senden(self, command_id: str, kind: str, done: Done) -> Done | Failed | None:
        assert done.inventur is not None
        if self._send_inventory is None:
            return Failed("intern", INTERN_TEXT)
        try:
            gesendet = self._send_inventory(command_id, done.inventur)
        except ValueError as error:
            logger.error("Inventur zu Befehl %s nicht gesendet (%s)", command_id, type(error).__name__)
            return Failed("intern", INVENTUR_TEXT)
        if gesendet:
            return Done(done.result)
        self._pending[command_id] = (kind, Waiting(lambda: done, float("inf")))
        return None

    def _store(self, command_id: str, entry: Entry) -> None:
        self._done.pop(command_id, None)
        self._done[command_id] = entry
        while len(self._done) > DONE_KEEP:
            del self._done[next(iter(self._done))]
        write_json(self._path, [[cid, *value] for cid, value in self._done.items()])


def _valid_hint(key, value) -> bool:
    return (isinstance(key, str) and isinstance(value, dict) and value.get("stufe") in wire.HINWEIS_STUFEN
            and isinstance(value.get("text"), str))


class Hinweise:
    """Befehl hinweis (Spec 3.4): der Server setzt oder loescht einen Hinweis am Geraet (key, stufe, text; leerer Text
    = loeschen), z. B. geraet_doppelt. Anzeigen macht der Host (Gateway: Seite, HA: Reparatur-Hinweis, 5c)."""

    def __init__(self, device_dir: Path) -> None:
        self._path = device_dir / HINWEISE_FILE
        raw = read_json(self._path)
        self._items: dict[str, dict] = (
            {key: value for key, value in raw.items() if _valid_hint(key, value)} if isinstance(raw, dict) else {})

    def handler(self, payload: dict) -> Outcome:
        key, stufe, text = payload.get("key"), payload.get("stufe"), payload.get("text")
        if not (isinstance(key, str) and 0 < len(key) <= HINWEIS_KEY_MAX_CHARS):
            raise InvalidPayload("key fehlt oder ist zu lang.")
        if stufe not in wire.HINWEIS_STUFEN:
            raise InvalidPayload("stufe ist unbekannt.")
        if not isinstance(text, str) or len(text) > HINWEIS_TEXT_MAX_CHARS:
            raise InvalidPayload("text fehlt oder ist zu lang.")
        if text:
            self._items[key] = {"stufe": stufe, "text": text}
        else:
            self._items.pop(key, None)
        write_json(self._path, self._items)
        return Done({})

    def alle(self) -> tuple[dict, ...]:
        return tuple({"key": key, **value} for key, value in sorted(self._items.items()))
