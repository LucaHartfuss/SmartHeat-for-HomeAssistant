"""Knopf-Dienst des Gateways (GP8, FR-21, GW 7): liest den Einschaltknopf des Pi 5 (KEY_POWER) selbst, logind ignoriert ihn
(`logind/smartheat.conf`). Zwei Handlungen, beide nur am Geraet:

- Code: genau ein Druck von hoechstens 2,0 s, danach 2,0 s kein Druecken. Der Dienst schreibt `host/knopf.json`
  {"seq": n, "ts": ISO}; der Container zeigt einen neuen Code (Task 8). Jeder Druck einer Folge, ein zu langer Druck oder
  ein Druck mit Gefolge ergibt keinen Code.
- Werksreset (Regel 4, freigegebene Parameter in den Konstanten unten): fuenf kurze Druecke (je <= 1,0 s, Pause <= 2,0 s,
  das fuenfte Druecken <= 10,0 s nach dem ersten) -> 60 s Fenster ab dem fuenften Loslassen; darin bestaetigt ein Druck von
  3,0 bis 4,5 s, dessen Druecken mindestens 5,0 s nach dem fuenften Loslassen liegt (gewertet beim Loslassen). Jeder
  andere Druck im Fenster wird ignoriert. Nur dann ruft der Dienst den eingespeisten `reset` (im Betrieb
  `werksreset.ausfuehren`, `werksreset_fuer`).

`KnopfFolge` ist rein (Uhr von aussen, Sekunden einer monotonen Uhr; intern ganze Mikrosekunden, damit die Kanten
exakt gelten). Gleichstand an einer Kante: Pause und Fenster sind einschliesslich (Pause genau 2,0 s gehoert noch zur
Folge, das Loslassen genau 60,0 s nach dem fuenften noch ins Fenster); Code und Verfallen kommen erst danach. Die
Ereignis-Zeitstempel des Kernels stammen aus CLOCK_MONOTONIC (EVIOCSCLOCKID): kein Sprung durch NTP, dieselbe Uhr wie
`time.monotonic` im Takt. Ausgaben: `host/knopf.json` (Code) und `/run/smartheat/led_knopf.json` (Reset-Hinweise fuer die
LED, `led.py`); ein Neustart oder Stromzyklus loescht den Zustand, er oeffnet nie ein Fenster."""
import fcntl
import logging
import os
import re
import select
import signal
import struct
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from smartheat_core import dateien

from smartheat_host import bundles, led, lesen, werksreset

logger = logging.getLogger("smartheat_host.knopf")

# --- Parameter der freigegebenen Folge (Regel 4, GP8): keine Aenderung ohne Rueckfrage beim Nutzer ---
CODE_DRUCK_MAX = 2.0  # hoechste Dauer des einen Drucks fuer einen Code
CODE_RUHE = 2.0  # danach so lange kein Druecken; zugleich hoechster Abstand zwischen Loslassen und naechstem Druecken
#                  in der Folge (komplementaer: bis einschliesslich 2,0 s gehoert das Druecken zur Folge, danach folgt der Code)
KURZ_MAX = 1.0  # hoechste Dauer eines Drucks der Reset-Folge
FOLGE_FENSTER = 10.0  # das fuenfte Druecken hoechstens so lange nach dem ersten
FOLGE_DRUECKE = 5
BEREIT_SEKUNDEN = 60.0  # Fenster ab dem fuenften Loslassen
BESTAETIGUNG_FRUEHESTENS = 5.0  # Druecken der Bestaetigung fruehestens so lange nach dem fuenften Loslassen
HALTEN_MIN = 3.0
HALTEN_MAX = 4.5

# --- Betrieb (keine Sicherheitsgrenzen) ---
TAKT_SEKUNDEN = 0.1  # laengste Wartezeit auf ein Ereignis; danach folgt ein Takt der Folge (Code kommt hoechstens so spaet)
RESET_LAEUFT_SEKUNDEN = 600.0  # Obergrenze fuer das LED-Muster reset_laeuft; der Reset startet neu oder loescht es
STOERUNG_SEKUNDEN = 5.0  # LED stoerung nach einem gescheiterten Reset oder Code (GW 15)

EV_KEY = 1
KEY_POWER = 116
# ioctl-Nummern von evdev: _IOW('E', nr, int) = (1 << 30) | (4 << 16) | (0x45 << 8) | nr
EVIOCGRAB = 0x40044590  # nr 0x90
EVIOCSCLOCKID = 0x400445A0  # nr 0xa0
_EREIGNIS = struct.Struct("llHHi")  # struct input_event: Zeit (Sekunden, Mikrosekunden), Typ, Code, Wert
_LESEN_EREIGNISSE = 32

_LEER, _FOLGE, _BEREIT = "leer", "folge", "bereit"


def _us(sekunden: float) -> int:
    return round(sekunden * 1_000_000)


_CODE_DRUCK_MAX, _CODE_RUHE, _KURZ_MAX = _us(CODE_DRUCK_MAX), _us(CODE_RUHE), _us(KURZ_MAX)
_FOLGE_FENSTER, _BEREIT_US = _us(FOLGE_FENSTER), _us(BEREIT_SEKUNDEN)
_BESTAETIGUNG_FRUEHESTENS, _HALTEN_MIN, _HALTEN_MAX = _us(BESTAETIGUNG_FRUEHESTENS), _us(HALTEN_MIN), _us(HALTEN_MAX)


class KnopfFolge:
    """Erkennt aus Druecken, Loslassen und Zeit die Ereignisse `code`, `reset_bereit`, `reset_verfallen`,
    `reset_bestaetigt`. Jede Methode nimmt die Zeit `t` (Sekunden, nicht fallend) und liefert die Ereignisse in
    Reihenfolge; verstrichene Zeit wird dabei immer zuerst gewertet (ein ausbleibender Takt verliert nichts)."""

    def __init__(self) -> None:
        self._zustand = _LEER
        self._gedrueckt: int | None = None  # Beginn des gehaltenen Drucks
        self._im_fenster = False  # der gehaltene Druck begann im Reset-Fenster
        self._anzahl = 0  # fertige Druecke der Folge
        self._erster = 0  # Beginn des ersten Drucks
        self._letztes_loslassen = 0
        self._folge_gueltig = True  # alle Druecke kurz, Pausen und 10-s-Fenster eingehalten
        self._code_moeglich = False  # der erste Druck war nicht laenger als CODE_DRUCK_MAX
        self._bereit_ab = 0  # fuenftes Loslassen

    def druecken(self, t: float) -> list[str]:
        jetzt = _us(t)
        ereignisse = self._zeit(jetzt)
        if self._gedrueckt is not None:
            return ereignisse  # der Kernel meldet kein zweites Druecken ohne Loslassen; das erste gilt weiter
        self._gedrueckt = jetzt
        self._im_fenster = self._zustand == _BEREIT
        if self._zustand == _LEER:
            self._zustand = _FOLGE
            self._anzahl, self._erster, self._folge_gueltig = 0, jetzt, True
        elif self._zustand == _FOLGE and self._anzahl == FOLGE_DRUECKE - 1 and jetzt - self._erster > _FOLGE_FENSTER:
            self._folge_gueltig = False  # das fuenfte Druecken kommt zu spaet
        return ereignisse

    def loslassen(self, t: float) -> list[str]:
        jetzt = _us(t)
        ereignisse = self._zeit(jetzt)
        if self._gedrueckt is None:
            return ereignisse  # Loslassen ohne Druecken
        beginn, self._gedrueckt = self._gedrueckt, None
        dauer = jetzt - beginn
        if self._im_fenster:
            if (self._zustand == _BEREIT and beginn - self._bereit_ab >= _BESTAETIGUNG_FRUEHESTENS
                    and _HALTEN_MIN <= dauer <= _HALTEN_MAX):
                self._zustand = _LEER
                ereignisse.append("reset_bestaetigt")
            return ereignisse  # jeder andere Druck im Fenster (auch eines inzwischen verfallenen) wird ignoriert
        self._anzahl += 1
        self._letztes_loslassen = jetzt
        if self._anzahl == 1:
            self._code_moeglich = dauer <= _CODE_DRUCK_MAX
        if dauer > _KURZ_MAX:
            self._folge_gueltig = False
        if self._anzahl == FOLGE_DRUECKE and self._folge_gueltig:
            self._zustand, self._bereit_ab = _BEREIT, jetzt
            ereignisse.append("reset_bereit")
        return ereignisse

    def takt(self, t: float) -> list[str]:
        return self._zeit(_us(t))

    def _zeit(self, jetzt: int) -> list[str]:
        """Zeitgesteuerte Uebergaenge: Ruhe nach dem letzten Loslassen (Code oder stilles Ende), Fensterende."""
        if self._zustand == _FOLGE and self._gedrueckt is None:
            if jetzt - self._letztes_loslassen > _CODE_RUHE:
                self._zustand = _LEER
                if self._anzahl == 1 and self._code_moeglich:
                    return ["code"]
        elif self._zustand == _BEREIT and jetzt - self._bereit_ab > _BEREIT_US:
            self._zustand = _LEER
            return ["reset_verfallen"]
        return []


def finde_knopf(devices: Path = Path("/proc/bus/input/devices"), *, wortbreite: int = struct.calcsize("l") * 8) -> Path | None:
    """Event-Datei des ersten Eingabegeraets, dessen KEY-Bitmaske KEY_POWER (Bit 116) traegt. `/proc/bus/input/devices` gibt
    die Maske als Woerter zu `wortbreite` Bits aus (hoechstes zuerst, ohne Fuellnullen)."""
    try:
        text = devices.read_text(errors="replace")
    except OSError:
        return None
    for block in re.split(r"\n\s*\n", text):
        handler = re.search(r"^H: Handlers=.*\bevent(\d+)\b", block, re.MULTILINE)
        key = re.search(r"^B: KEY=([0-9a-fA-F ]+)$", block, re.MULTILINE)
        if not handler or not key:
            continue
        woerter = key.group(1).split()
        index = KEY_POWER // wortbreite
        if index < len(woerter) and (int(woerter[len(woerter) - 1 - index], 16) >> (KEY_POWER % wortbreite)) & 1:
            return Path("/dev/input") / f"event{handler.group(1)}"
    return None


def oeffne(pfad: Path) -> int:
    """Nicht blockierend, exklusiv (EVIOCGRAB: kein anderer Leser, auch logind nicht) und mit Zeitstempeln aus
    CLOCK_MONOTONIC (EVIOCSCLOCKID)."""
    fd = os.open(pfad, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        fcntl.ioctl(fd, EVIOCSCLOCKID, struct.pack("i", time.CLOCK_MONOTONIC))
        fcntl.ioctl(fd, EVIOCGRAB, 1)
    except BaseException:
        os.close(fd)
        raise
    return fd


def lies_ereignisse(fd: int) -> Iterator[tuple[int, int]]:
    """Liefert (Zeit in Mikrosekunden, Wert) der Tastenereignisse KEY_POWER mit Wert 1 (Druecken) und 0 (Loslassen), bis
    nichts mehr bereitliegt. Wiederholungen (2) und alle anderen Ereignisse werden verworfen. Meldet das Geraet das Ende
    (verschwunden, geschlossen), gilt das als Fehler (EOFError bzw. OSError): der Dienst endet laut."""
    rest = b""
    while True:
        try:
            daten = os.read(fd, _EREIGNIS.size * _LESEN_EREIGNISSE)
        except BlockingIOError:
            return
        if not daten:
            raise EOFError("Das Knopf-Geraet meldet kein Ereignis mehr")
        rest += daten
        while len(rest) >= _EREIGNIS.size:
            sekunden, mikro, typ, code, wert = _EREIGNIS.unpack_from(rest)
            rest = rest[_EREIGNIS.size:]
            if typ == EV_KEY and code == KEY_POWER and wert in (0, 1):
                yield sekunden * 1_000_000 + mikro, wert


class Ausgabe:
    """Setzt die Ereignisse der Folge um: Code -> host/knopf.json, Reset-Hinweise -> led_knopf.json, Reset -> `reset`."""

    def __init__(self, root: Path, led_datei: Path | None = None, wanduhr: Callable[[], float] = time.time) -> None:
        self._knopf_datei = root / "host" / "knopf.json"
        self._led_datei = led.KNOPF_DATEI if led_datei is None else led_datei
        self._wanduhr = wanduhr

    def behandeln(self, ereignis: str, reset: Callable[[], None]) -> None:
        if ereignis == "code":
            self._code()
        elif ereignis == "reset_bereit":
            self._led("reset_bereit", BEREIT_SEKUNDEN)
        elif ereignis == "reset_verfallen":
            self._led_loeschen()
        elif ereignis == "reset_bestaetigt":
            self._reset(reset)
        else:
            raise ValueError(f"Unbekanntes Ereignis: {ereignis!r}")

    def _code(self) -> None:
        alt = lesen.read_json(self._knopf_datei)
        seq = alt.get("seq") if isinstance(alt, dict) else None
        seq = seq + 1 if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0 else 1
        ts = datetime.fromtimestamp(self._wanduhr(), UTC).isoformat(timespec="seconds")
        try:
            dateien.schreiben_json(self._knopf_datei, {"seq": seq, "ts": ts}, modus=dateien.LESBAR)
        except OSError:
            logger.exception("host/knopf.json nicht geschrieben, Code geht verloren")
            self._led("stoerung", STOERUNG_SEKUNDEN)
            return
        logger.info("Knopf: Code angefordert, seq=%d", seq)

    def _reset(self, reset: Callable[[], None]) -> None:
        logger.warning("Knopf: Werksreset bestaetigt")
        self._led("reset_laeuft", RESET_LAEUFT_SEKUNDEN)
        try:
            reset()
        except Exception:
            logger.exception("Werksreset gescheitert")
            self._led("stoerung", STOERUNG_SEKUNDEN)
            return
        self._led_loeschen()

    def _led(self, muster: str, sekunden: float) -> None:
        try:
            dateien.schreiben_json(self._led_datei, {"muster": muster, "bis": self._wanduhr() + sekunden},
                                   modus=dateien.LESBAR)
        except OSError:
            logger.exception("led_knopf.json nicht geschrieben")

    def _led_loeschen(self) -> None:
        try:
            self._led_datei.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.exception("led_knopf.json nicht geloescht")


def laufe(fd: int, folge: KnopfFolge, behandeln: Callable[[str], None], uhr: Callable[[], float] = time.monotonic,
          ende: Callable[[], bool] = lambda: False, takt: float = TAKT_SEKUNDEN) -> None:
    """Liest Ereignisse und tackt die Folge, bis `ende()` wahr wird oder das Geraet verschwindet (EOFError/OSError)."""
    while not ende():
        select.select([fd], [], [], takt)
        for mikro, wert in lies_ereignisse(fd):
            zeit = mikro / 1_000_000
            for ereignis in folge.druecken(zeit) if wert == 1 else folge.loslassen(zeit):
                behandeln(ereignis)
        for ereignis in folge.takt(uhr()):
            behandeln(ereignis)


def _terminate(signum, frame) -> None:  # pragma: no cover - Signalhandler
    raise SystemExit(0)


def _wurzel() -> Path:
    return Path(os.environ.get("SHG_ROOT", "/var/lib/smartheat"))


def werksreset_fuer(root: Path) -> Callable[[], None]:
    """Der Reset des Dienstes: der Werksreset mit echtem Compose, echter Uhr, `systemctl reboot` und `journalctl`. Der einzige
    Aufruf von `werksreset.ausfuehren` im Gateway (`test_reset_nur_ueber_knopf`)."""
    compose = bundles.ComposeRunner(root)

    def reset() -> None:
        werksreset.ausfuehren(root, compose, warte=werksreset.warten, jetzt=time.time, neustart=werksreset.neustart_systemd,
                              journal_leeren=werksreset.journal_leeren_systemd)

    return reset


def main(*, reset: Callable[[], None]) -> None:  # systemd-Einstieg
    logging.basicConfig(level=logging.INFO)
    root = _wurzel()
    pfad = finde_knopf()
    if pfad is None:
        raise SystemExit("Kein Knopf gefunden (kein Eingabegeraet mit KEY_POWER in /proc/bus/input/devices)")
    # Eine Anfrage in der Phase angefordert aus einem frueheren Lauf dieses Dienstes (abgebrochen) darf der Container nicht
    # mehr beantworten: verwerfen, bevor die erste Folge kommt. Eine Anfrage in der Phase loeschen bleibt fuer den Erststart.
    werksreset.verwerfen_angefordert(root, time.time)
    # systemd beendet mit SIGTERM: als SystemExit abfangen, damit der Deskriptor (und mit ihm EVIOCGRAB) freigegeben wird.
    # Waehrend des Loeschens im Werksreset schiebt `werksreset.ausfuehren` das Signal auf, bis alles getan ist.
    signal.signal(signal.SIGTERM, _terminate)
    fd = oeffne(pfad)
    try:
        ausgabe = Ausgabe(root)
        laufe(fd, KnopfFolge(), lambda ereignis: ausgabe.behandeln(ereignis, reset))
    finally:
        os.close(fd)


if __name__ == "__main__":
    main(reset=werksreset_fuer(_wurzel()))
