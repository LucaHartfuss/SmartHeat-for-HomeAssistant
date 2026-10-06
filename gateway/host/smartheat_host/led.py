"""LED des Gateways (Spec G2b-1 5, G2 8.5): spielt je Agent-Zustand ein Muster auf der ACT-LED. Zustand aus
data/agent_state.json (Agent schreibt mindestens alle 60 s); aelter als 5 min oder unlesbar = stoerung. Die Schluessel
von PATTERNS sind genau die Zustaende des Agenten (wire.AGENT_STATES)."""
import json
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path

STALE_SECONDS = 300
_PULSE = 0.15
PATTERNS: dict[str, tuple[tuple[bool, float], ...]] = {
    "startet": ((True, 0.1), (False, 0.1)),
    "nicht_uebernommen": ((True, _PULSE), (False, _PULSE), (True, _PULSE), (False, 2.0 - 3 * _PULSE)),
    "wartet_auf_einrichtung": ((True, _PULSE), (False, _PULSE), (True, _PULSE), (False, 2.0 - 3 * _PULSE)),
    "keine_verbindung": ((True, 0.5), (False, 0.5)),
    "regelt": ((True, 1.0),),
    "stoerung": ((True, _PULSE), (False, _PULSE)) * 2 + ((True, _PULSE), (False, 2.0 - 5 * _PULSE)),
}


def state_from_file(path: Path, now: Callable[[], float] = time.time) -> str:
    """Zustand des Agenten; veraltete, unlesbare oder unbekannte Angaben zeigen stoerung."""
    try:
        if now() - path.stat().st_mtime > STALE_SECONDS:
            return "stoerung"
        zustand = json.loads(path.read_text()).get("zustand")
    except (OSError, ValueError, AttributeError):
        return "stoerung"
    return zustand if isinstance(zustand, str) and zustand in PATTERNS else "stoerung"


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
        for on, seconds in PATTERNS[state]:
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


def main() -> None:  # pragma: no cover - systemd-Einstieg
    base = Path(os.environ["SHG_LED"]) if os.environ.get("SHG_LED") else find_led()
    if base is None:
        raise SystemExit("Keine LED gefunden (/sys/class/leds/ACT oder led0)")
    state_path = Path(os.environ.get("SHG_ROOT", "/var/lib/smartheat")) / "data" / "agent_state.json"
    # systemd beendet mit SIGTERM: als SystemExit abfangen, damit der Trigger im finally zurueckgesetzt wird.
    signal.signal(signal.SIGTERM, _terminate)
    led = Led(base)
    led.take()
    try:
        play(led, lambda: state_from_file(state_path), time.monotonic, time.sleep)
    finally:
        led.restore()


if __name__ == "__main__":
    main()
