"""Werksreset des Gateways (Regel 4, SG-01 Geraeteteil, GP8, GPC9, GW 7.2 und 15): loescht die Rastung, die Schluessel und das
Zigbee-Netz des Kunden. Der Weg dorthin ist allein die Folge am Knopf (`knopf.py` ruft `ausfuehren`); `beim_start` fuehrt nur
einen unterbrochenen Reset zu Ende (`firstboot.py`). Kein Befehl, keine Route, kein Weg ueber Server oder LAN-Seite.

Ablauf von `ausfuehren` (jede Phase steht vor ihrer Wirkung in `host/werksreset.json`; der Waechter pausiert, solange die
Datei liegt):
1. Abweisen, solange der Systemwechsel in installiere, installiert oder probestart steht (gleiche Lesart wie der Waechter).
2. Phase `angefordert`: der Container setzt die Ursprungswerte zurueck wie beim Abmelden und antwortet in
   `data/werksreset_bereit.json` {seq, ts, ursprungswerte} (nicht eingerichtet: sofort `false`). Hoechstens 120 s warten
   (GPC9); ohne Antwort (Container tot, kein Treiber) wird trotzdem geloescht, die Anlage bleibt dann auf den zuletzt
   geschriebenen Werten innerhalb der lokalen Grenzen (GW 15).
3. Phase `loeschen`: Compose anhalten (unter der Compose-Sperre), Inhalte von `data/`, `bus/credentials/`, `bus/mosquitto/`,
   `bus/mosquitto-daten/`, `zigbee2mqtt/` und `host/knopf.json` loeschen, Journal leeren, Protokoll schreiben, Anfrage
   loeschen, Neustart. `werk`, `bundles/`, `updater/`, `archive/`, der uebrige `host/`, der Docker-Speicher und der aktive
   Slot bleiben.

Kein halber Reset: ein Stromausfall oder Absturz in `angefordert` wird beim naechsten Start verworfen (ein Stromzyklus
loest keinen Reset aus), in `loeschen` fuehrt `beim_start` das Loeschen zu Ende, bevor irgendetwas startet. Ein SIGTERM
waehrend des Wartens verwirft die Anfrage; ab Phase `loeschen` wird er aufgeschoben, bis das Loeschen und der Neustart
getan sind. Scheitert das Anhalten oder Loeschen im laufenden Betrieb, bleibt die Phase stehen und der Neustart holt es nach.
Geloescht wird nie einem Symlink nach (Ordner und Eintraege werden per Dateideskriptor mit O_NOFOLLOW behandelt)."""
import contextlib
import errno
import logging
import os
import shutil
import signal
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from smartheat_core import dateien

from smartheat_host import lesen, updater, waechter

logger = logging.getLogger("smartheat_host.werksreset")

WARTE_SEKUNDEN = 120.0  # Frist fuer das Zuruecksetzen der Ursprungswerte (GPC9)
WARTE_TAKT_SEKUNDEN = 1.0
JOURNAL_FRIST_SEKUNDEN = 60.0
NEUSTART_FRIST_SEKUNDEN = 30.0
PROTOKOLL_MAX = 20
ANGEFORDERT, LOESCHEN = "angefordert", "loeschen"
#: Inhalte (die Ordner selbst bleiben; `ordner_angleichen` legt Unterordner wieder an), relativ zu SHG_ROOT
LOESCH_ORDNER = ("data", "bus/credentials", "bus/mosquitto", "bus/mosquitto-daten", "zigbee2mqtt")
LOESCH_DATEIEN = ("host/knopf.json",)


class Abgewiesen(Exception):
    """Der Reset beginnt nicht: das System wechselt gerade."""


class LoeschFehler(Exception):
    """Mindestens ein Eintrag liess sich nicht loeschen (die uebrigen sind weg)."""


def _anfrage_pfad(root: Path) -> Path:
    return root / "host" / "werksreset.json"


def _bereit_pfad(root: Path) -> Path:
    return root / "data" / "werksreset_bereit.json"


def _protokoll_pfad(root: Path) -> Path:
    return root / "host" / "werksreset_protokoll.json"


def _iso(sekunden: float) -> str:
    return datetime.fromtimestamp(sekunden, UTC).isoformat(timespec="seconds")


def _zeit(text: object) -> datetime | None:
    try:
        wert = datetime.fromisoformat(text) if isinstance(text, str) else None
    except ValueError:
        return None
    return wert if wert is not None and wert.tzinfo is not None else None


def _int(wert: object) -> bool:
    return isinstance(wert, int) and not isinstance(wert, bool)


# --- Anfrage und Protokoll ---

def _anfrage_lesen(root: Path) -> dict | None:
    """Die Anfrage, wenn sie vollstaendig ist (seq, ts, Phase angefordert oder loeschen); sonst None."""
    roh = lesen.read_json(_anfrage_pfad(root))
    if (isinstance(roh, dict) and _int(roh.get("seq")) and _zeit(roh.get("ts")) is not None
            and roh.get("phase") in (ANGEFORDERT, LOESCHEN)):
        return roh
    return None


def _anfrage_loeschen(root: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        _anfrage_pfad(root).unlink()


def _protokoll(root: Path) -> list[dict]:
    roh = lesen.read_json(_protokoll_pfad(root))
    return [eintrag for eintrag in roh if isinstance(eintrag, dict)] if isinstance(roh, list) else []


def _protokollieren(root: Path, eintrag: dict) -> None:
    """Haengt den Eintrag an (hoechstens PROTOKOLL_MAX); ein Eintrag mit derselben seq und ts ersetzt den alten (ein zu
    Ende gefuehrter Reset schreibt nicht doppelt). Nur Zeitpunkt und Ergebnis, nie Eingaben (S-12)."""
    liste = [alt for alt in _protokoll(root) if not (
        "seq" in eintrag and (alt.get("seq"), alt.get("ts")) == (eintrag["seq"], eintrag["ts"]))]
    dateien.schreiben_json(_protokoll_pfad(root), [*liste, eintrag][-PROTOKOLL_MAX:], modus=dateien.LESBAR)


def _protokollieren_best_effort(root: Path, eintrag: dict) -> None:
    try:
        _protokollieren(root, eintrag)
    except OSError as fehler:
        logger.error("Protokoll des Werksresets nicht geschrieben (%s)", type(fehler).__name__)


def _naechste_seq(root: Path) -> int:
    folgen = [eintrag["seq"] for eintrag in _protokoll(root) if _int(eintrag.get("seq"))]
    return max(folgen, default=0) + 1


def verwerfen_angefordert(root: Path, jetzt: Callable[[], float]) -> bool:
    """Loescht eine Anfrage in der Phase `angefordert` (der Reset hat noch nichts geloescht) und protokolliert das; True,
    wenn eine verworfen wurde. Eine Anfrage in der Phase `loeschen` bleibt. Eine unbrauchbare Anfragedatei (kein
    vollstaendiger Inhalt, unbekannte Phase) wird ebenfalls entfernt: sie kann keinen Reset tragen."""
    pfad = _anfrage_pfad(root)
    if not os.path.lexists(pfad):
        return False
    anfrage = _anfrage_lesen(root)
    if anfrage is not None and anfrage["phase"] != ANGEFORDERT:
        return False
    with contextlib.suppress(FileNotFoundError):
        pfad.unlink()
    eintrag: dict = {"ts": _iso(jetzt()), "ergebnis": "verworfen"}
    if anfrage is not None:
        eintrag["seq"] = anfrage["seq"]
        eintrag["ts_anfrage"] = anfrage["ts"]
    _protokollieren_best_effort(root, eintrag)
    logger.warning("Werksreset: Anfrage verworfen (der Reset hatte noch nichts geloescht)")
    return True


# --- Loeschen ---

def _unterordner(eltern_fd: int, name: str) -> int | None:
    """Oeffnet `name` unter `eltern_fd` ohne einem Symlink zu folgen; fehlt er: None. Ist er ein Symlink oder eine Datei,
    wird der Eintrag selbst entfernt (None): der Ordner entsteht bei `ordner_angleichen` neu."""
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=eltern_fd)
    except FileNotFoundError:
        return None
    except OSError as fehler:
        if fehler.errno not in (errno.ELOOP, errno.ENOTDIR):
            raise
        os.unlink(name, dir_fd=eltern_fd)
        return None


def _leeren(ordner_fd: int) -> list[str]:
    """Loescht alle Eintraege im Ordner (Unterordner rekursiv, Symlinks als Eintrag, nie ihrem Ziel nach); liefert die
    Namen der Eintraege, die bleiben."""
    uebrig = []
    for name in os.listdir(ordner_fd):
        try:
            if stat.S_ISDIR(os.stat(name, dir_fd=ordner_fd, follow_symlinks=False).st_mode):
                shutil.rmtree(name, dir_fd=ordner_fd)
            else:
                os.unlink(name, dir_fd=ordner_fd)
        except FileNotFoundError:
            continue
        except OSError as fehler:
            logger.error("Werksreset: %s nicht geloescht (%s)", name, type(fehler).__name__)
            uebrig.append(name)
    return uebrig


def _folge_oeffnen(root_fd: int, teile: Sequence[str]) -> int | None:
    """Der Ordner unter `root_fd` entlang `teile` (ohne Symlinks); fehlt ein Teil: None. Das Ergebnis schliesst der Aufrufer."""
    fd = os.dup(root_fd)
    try:
        for teil in teile:
            neu = _unterordner(fd, teil)
            if neu is None:
                return None
            os.close(fd)
            fd = neu
        return fd
    except BaseException:
        os.close(fd)
        raise


def _dateien_loeschen(root: Path) -> None:
    """Loescht die Inhalte der LOESCH_ORDNER und die LOESCH_DATEIEN unter `root`; wirft LoeschFehler, wenn etwas bleibt."""
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    uebrig = []
    try:
        for rel in LOESCH_ORDNER:
            *eltern, name = rel.split("/")
            eltern_fd = _folge_oeffnen(root_fd, eltern)
            if eltern_fd is None:
                continue
            try:
                ordner_fd = _unterordner(eltern_fd, name)
                if ordner_fd is None:
                    continue
                try:
                    uebrig += [f"{rel}/{eintrag}" for eintrag in _leeren(ordner_fd)]
                finally:
                    os.close(ordner_fd)
            finally:
                os.close(eltern_fd)
        for rel in LOESCH_DATEIEN:
            *eltern, name = rel.split("/")
            eltern_fd = _folge_oeffnen(root_fd, eltern)
            if eltern_fd is None:
                continue
            try:
                os.unlink(name, dir_fd=eltern_fd)
            except FileNotFoundError:
                pass
            except OSError as fehler:
                logger.error("Werksreset: %s nicht geloescht (%s)", rel, type(fehler).__name__)
                uebrig.append(rel)
            finally:
                os.close(eltern_fd)
    finally:
        os.close(root_fd)
    if uebrig:
        raise LoeschFehler(f"{len(uebrig)} Eintraege nicht geloescht")


def _abschliessen(root: Path, compose, anfrage: dict, *, jetzt: Callable[[], float],
                  journal_leeren: Callable[[], None]) -> None:
    """Phase `loeschen`: Compose anhalten und loeschen unter der Compose-Sperre (ein `up` des Updaters oder Erststarts
    ueberlappt nie), dann Journal leeren, Protokoll schreiben und die Anfrage loeschen. Idempotent: jeder Schritt darf
    wiederholt werden."""
    with compose.sperre():
        version = updater.State.load(root / "updater" / "state.json").current
        if version is not None:
            compose.down(version)
        _dateien_loeschen(root)
    journal = True
    try:
        journal_leeren()
    except Exception as fehler:  # das Journal ist Beiwerk des Resets: die Kundendaten sind weg, das Ergebnis steht im Protokoll
        journal = False
        logger.error("Werksreset: Journal nicht geleert (%s)", type(fehler).__name__)
    _protokollieren(root, {"seq": anfrage["seq"], "ts": anfrage["ts"], "ergebnis": "ausgefuehrt",
                           "fertig": _iso(jetzt()), "ursprungswerte": anfrage.get("ursprungswerte") is True,
                           "journal": journal})
    _anfrage_loeschen(root)
    logger.warning("Werksreset ausgefuehrt (Ursprungswerte zurueckgesetzt: %s)",
                   "ja" if anfrage.get("ursprungswerte") is True else "nein")


# --- Ablauf ---

def _antwort(root: Path, seq: int, ts: str) -> bool | None:
    """`ursprungswerte` der Antwort des Containers auf genau diese Anfrage (gleiche seq, ts nicht vor der Anfrage);
    sonst None. Eine alte Antwort (andere seq oder frueherer Zeitpunkt) zaehlt nicht."""
    roh = lesen.read_json(_bereit_pfad(root))
    if not isinstance(roh, dict) or roh.get("seq") != seq or not _int(roh.get("seq")):
        return None
    antwort, anfrage = _zeit(roh.get("ts")), _zeit(ts)
    if antwort is None or anfrage is None or antwort < anfrage or not isinstance(roh.get("ursprungswerte"), bool):
        return None
    return roh["ursprungswerte"]


@contextlib.contextmanager
def _sigterm_aufschieben():
    """Haelt ein SIGTERM zurueck, bis der Block fertig ist, und stellt es dann zu (der alte Handler wirkt). Nur im
    Hauptthread moeglich; sonst ohne Wirkung."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    empfangen: list[int] = []
    alt = signal.signal(signal.SIGTERM, lambda nummer, frame: empfangen.append(nummer))
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, alt)
        if empfangen:
            os.kill(os.getpid(), signal.SIGTERM)


def ausfuehren(root: Path, compose, *, warte: Callable[[Callable[[], bool], float], bool], jetzt: Callable[[], float],
               neustart: Callable[[], None], journal_leeren: Callable[[], None]) -> dict:
    """Der Werksreset nach Bestaetigung der Folge am Knopf (Modulkopf). `warte(bedingung, frist)` wartet hoechstens `frist`
    Sekunden auf die Bedingung und liefert, ob sie erfuellt wurde. Liefert {seq, ts, ursprungswerte, abgeschlossen}; wirft
    `Abgewiesen` (nichts geschehen) oder was das Schreiben der Anfrage wirft (nichts geschehen)."""
    state = updater.State.load(root / "updater" / "state.json")
    if waechter.systemwechsel(state):
        phase = state.system["phase"] if state.system else ""
        _protokollieren_best_effort(root, {"ts": _iso(jetzt()), "ergebnis": "abgewiesen", "grund": "systemwechsel"})
        logger.warning("Werksreset abgewiesen: Systemwechsel laeuft (Phase %s)", phase)
        raise Abgewiesen(f"Systemwechsel laeuft (Phase {phase})")
    seq, ts = _naechste_seq(root), _iso(jetzt())
    anfrage = {"seq": seq, "ts": ts, "phase": ANGEFORDERT}
    dateien.schreiben_json(_anfrage_pfad(root), anfrage, modus=dateien.LESBAR)
    logger.warning("Werksreset angefordert, warte bis %.0f s auf das Zuruecksetzen der Ursprungswerte", WARTE_SEKUNDEN)
    try:
        beantwortet = warte(lambda: _antwort(root, seq, ts) is not None, WARTE_SEKUNDEN)
    except BaseException:  # SIGTERM (SystemExit) oder ein Fehler beim Warten: noch nichts geloescht
        verwerfen_angefordert(root, jetzt)
        raise
    ursprungswerte = beantwortet and _antwort(root, seq, ts) is True
    if not beantwortet:
        logger.warning("Werksreset: keine Antwort des Containers, es wird trotzdem geloescht")
    with _sigterm_aufschieben():
        anfrage = {"seq": seq, "ts": ts, "phase": LOESCHEN, "ursprungswerte": ursprungswerte}
        dateien.schreiben_json(_anfrage_pfad(root), anfrage, modus=dateien.LESBAR)
        try:
            _abschliessen(root, compose, anfrage, jetzt=jetzt, journal_leeren=journal_leeren)
        except Exception as fehler:
            # Die Phase bleibt stehen: der Neustart fuehrt den Reset in `beim_start` zu Ende, bevor etwas startet.
            logger.error("Werksreset: Anhalten oder Loeschen gescheitert (%s), der Neustart fuehrt es zu Ende",
                         type(fehler).__name__)
            neustart()
            return {"seq": seq, "ts": ts, "ursprungswerte": ursprungswerte, "abgeschlossen": False}
        neustart()
    return {"seq": seq, "ts": ts, "ursprungswerte": ursprungswerte, "abgeschlossen": True}


def beim_start(root: Path, compose, *, jetzt: Callable[[], float], journal_leeren: Callable[[], None]) -> bool:
    """Vor `compose up` im Erststart (`smartheat-firstboot`): eine Anfrage in `angefordert` wird verworfen, in `loeschen`
    wird der Reset zu Ende gefuehrt. True, wenn ein Reset abgeschlossen wurde (der Aufrufer gleicht dann die Ordner an).
    Scheitert das Anhalten oder Loeschen, geht der Fehler an den Aufrufer: nichts startet auf halb geloeschten Daten."""
    anfrage = _anfrage_lesen(root)
    if anfrage is None or anfrage["phase"] == ANGEFORDERT:
        verwerfen_angefordert(root, jetzt)
        return False
    logger.warning("Werksreset: unterbrochener Reset wird zu Ende gefuehrt")
    _abschliessen(root, compose, anfrage, jetzt=jetzt, journal_leeren=journal_leeren)
    return True


# --- Einstieg des Knopf-Dienstes (echte Wirkung; Tests speisen Attrappen ein) ---

def warten(bedingung: Callable[[], bool], frist: float, *, uhr: Callable[[], float] = time.monotonic,
           schlaf: Callable[[float], None] = time.sleep, takt: float = WARTE_TAKT_SEKUNDEN) -> bool:
    """Prueft die Bedingung im Takt bis zur Frist (monotone Uhr); True, sobald sie erfuellt ist."""
    ende = uhr() + frist
    while True:
        if bedingung():
            return True
        rest = ende - uhr()
        if rest <= 0:
            return False
        schlaf(min(takt, rest))


def neustart_systemd() -> None:
    subprocess.run(["systemctl", "reboot"], check=True, capture_output=True, timeout=NEUSTART_FRIST_SEKUNDEN)


def journal_leeren_systemd() -> None:
    """Journal und Container-Zeilen weg (FR-38): erst rotieren (die aktive Datei wird archiviert), dann alles Archivierte."""
    for argumente in (["--rotate"], ["--vacuum-time=1s"]):
        subprocess.run(["journalctl", *argumente], check=True, capture_output=True, timeout=JOURNAL_FRIST_SEKUNDEN)
