"""LED des Gateways: spielt je Zustand des Geraetekerns (wire.ZUSTAENDE) ein Muster
auf der ACT-LED. Zustand aus data/agent_state.json (die Laufzeit schreibt mindestens alle 60 s; der Name stammt aus
0.5.0); aelter als 5 min oder unlesbar = stoerung. Die Schluessel von PATTERNS sind genau die Zustaende des
Geraetekerns. Dazu die Muster des Knopfs (KNOPF_MUSTER, nur am Host, GP8) mit dem Vorrang
/run/smartheat/led_knopf.json (Knopf-Dienst, bis Wanduhr) > code_fenster in agent_state.json > Zustand."""
import json
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path

from smartheat_host import lesen

STALE_SECONDS = 300
#: Hinweise des Knopf-Dienstes (smartheat_host.knopf); in /run, ein Neustart loescht sie
KNOPF_DATEI = Path("/run/smartheat/led_knopf.json")
_PULSE = 0.15
PATTERNS: dict[str, tuple[tuple[bool, float], ...]] = {
    "startet": ((True, 0.1), (False, 0.1)),
    "nicht_uebernommen": ((True, _PULSE), (False, _PULSE), (True, _PULSE), (False, 2.0 - 3 * _PULSE)),
    "wartet_auf_einrichtung": ((True, _PULSE), (False, _PULSE), (True, _PULSE), (False, 2.0 - 3 * _PULSE)),
    "keine_verbindung": ((True, 0.5), (False, 0.5)),
    "regelt": ((True, 1.0),),
    "stoerung": ((True, _PULSE), (False, _PULSE)) * 2 + ((True, _PULSE), (False, 2.0 - 5 * _PULSE)),
    # Gesperrt = lang an, kurz aus (dauerhaft, Support noetig); update_noetig = zwei lange Pulse.
    "gesperrt": ((True, 1.7), (False, 0.3)),
    "update_noetig": ((True, 0.6), (False, 0.2), (True, 0.6), (False, 0.6)),
}
# Muster des Knopfs, nicht in wire.ZUSTAENDE (der Geraetekern kennt sie nicht): Reset-Folge bereit, Reset laeuft, Code
# sichtbar (Fenster der LAN-Seite offen).
KNOPF_MUSTER: dict[str, tuple[tuple[bool, float], ...]] = {
    "reset_bereit": ((True, 0.1), (False, 0.1)),
    "reset_laeuft": ((True, 1.0),),
    "code_sichtbar": ((True, 0.2), (False, 0.8)),
}
MUSTER = {**PATTERNS, **KNOPF_MUSTER}
#: Muster, die der Knopf-Dienst in led_knopf.json setzen darf
KNOPF_DATEI_MUSTER = ("reset_bereit", "reset_laeuft", "stoerung")


def state_from_file(path: Path, now: Callable[[], float] = time.time) -> str:
    """Zustand des Geraetekerns; veraltete, unlesbare oder unbekannte Angaben zeigen stoerung. Meldet die frische Datei
    `code_fenster: true`, zeigt die LED code_sichtbar (ein gueltiger Zustand vorausgesetzt)."""
    try:
        data, info = lesen.read_stat(path)  # data/ ist fuer Container schreibbar: begrenzt, ohne Symlink, ohne FIFO
        if now() - info.st_mtime > STALE_SECONDS:
            return "stoerung"
        inhalt = json.loads(data)
        zustand = inhalt.get("zustand")
        fenster = inhalt.get("code_fenster") is True
    except (OSError, ValueError, AttributeError):
        return "stoerung"
    if not (isinstance(zustand, str) and zustand in PATTERNS):
        return "stoerung"
    return "code_sichtbar" if fenster else zustand


def knopf_muster(path: Path, now: Callable[[], float] = time.time) -> str | None:
    """Muster aus led_knopf.json, solange `bis` (Wanduhr, Sekunden) nicht erreicht ist; sonst None. Die Datei liegt in /run
    und gehoert root; trotzdem begrenzt und ohne Symlink gelesen, Fehler zeigen keinen Hinweis (kein Log je 0,5 s)."""
    try:
        inhalt = json.loads(lesen.read_bytes(path))
        muster, bis = inhalt.get("muster"), inhalt.get("bis")
    except (OSError, ValueError, AttributeError):
        return None
    if not (isinstance(muster, str) and muster in KNOPF_DATEI_MUSTER):
        return None
    if isinstance(bis, bool) or not isinstance(bis, int | float) or now() >= bis:
        return None
    return muster


def muster_waehlen(state_path: Path, knopf_path: Path = KNOPF_DATEI, now: Callable[[], float] = time.time) -> str:
    """Vorrang: led_knopf.json > code_fenster in agent_state.json > Zustand."""
    return knopf_muster(knopf_path, now) or state_from_file(state_path, now)


def find_led(leds_dir: Path = Path("/sys/class/leds")) -> Path | None:
    for name in ("ACT", "led0"):
        if (leds_dir / name).is_dir():
            return leds_dir / name
    return None


class Led:
    def __init__(self, base: Path) -> None:
        self._base = base
        self._previous: str | None = None

    def take(self) -> None:
        """Merkt sich den aktiven Trigger (in eckigen Klammern) und schaltet auf none."""
        text = (self._base / "trigger").read_text()
        self._previous = next((word[1:-1] for word in text.split() if word.startswith("[") and word.endswith("]")),
                              None)
        (self._base / "trigger").write_text("none")

    def set(self, on: bool) -> None:
        (self._base / "brightness").write_text("1" if on else "0")

    def restore(self) -> None:
        if self._previous:
            (self._base / "trigger").write_text(self._previous)


def play(led: Led, read_state: Callable[[], str], clock: Callable[[], float], sleep: Callable[[float], None],
         steps: int | None = None) -> None:
    """Spielt Muster ab; der Zustand wird hoechstens alle 0,5 s neu gelesen. steps begrenzt die Schritte (Tests)."""
    state, read_at, done = read_state(), clock(), 0
    while steps is None or done < steps:
        for on, seconds in MUSTER[state]:
            if steps is not None and done >= steps:
                return
            led.set(on)
            sleep(seconds)
            done += 1
            if clock() - read_at >= 0.5:
                new_state, read_at = read_state(), clock()
                if new_state != state:
                    state = new_state
                    break


def _terminate(signum, frame) -> None:  # pragma: no cover - Signalhandler
    raise SystemExit(0)


def main() -> None:  # systemd-Einstieg
    base = find_led()
    if base is None:
        raise SystemExit("Keine LED gefunden (/sys/class/leds/ACT oder led0)")
    state_path = Path(os.environ.get("SHG_ROOT", "/var/lib/smartheat")) / "data" / "agent_state.json"
    # systemd beendet mit SIGTERM: als SystemExit abfangen, damit der Trigger im finally zurueckgesetzt wird.
    signal.signal(signal.SIGTERM, _terminate)
    led = Led(base)
    try:
        # take liegt im try: SIGTERM kann schon zwischen dem Schreiben von none und der Rueckkehr aus take eintreffen.
        led.take()
        play(led, lambda: muster_waehlen(state_path, KNOPF_DATEI), time.monotonic, time.sleep)
    finally:
        led.restore()


if __name__ == "__main__":
    main()
