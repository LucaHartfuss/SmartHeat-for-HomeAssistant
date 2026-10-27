"""Waechter fuer die Laufzeit (GA-09, FR-35): bleibt der Container `runtime` ungesund (Compose-Healthcheck: Seite antwortet
nicht oder der Regel-Worker lebt nicht mehr), startet der Host ihn neu. Der Container selbst kann das nicht (`restart:
unless-stopped` greift nur beim Ende des Prozesses, nicht bei `unhealthy`).

Takt 30 s. Drei ungesunde Pruefungen in Folge ergeben einen Neustart (`docker compose -p smartheat restart runtime`, Frist
120 s), hoechstens drei je Stunde; der vierte wird nicht ausgefuehrt (`aufgegeben`, ein ERROR im Journal) und kommt wieder,
sobald der aelteste Neustart eine Stunde zurueckliegt. Die Zaehler leben nur im Speicher: ein Neustart des Waechters beginnt
neu, die Uhr ist `time.monotonic` (kein Sprung durch NTP).

Pausiert (kein Neustart, die Folge beginnt neu), solange der Updater die Container selbst umstellt oder das System wechselt
(`updater/state.json`: `in_progress` oder `system.phase` in installiere, installiert, probestart) oder `host/werksreset.json`
liegt; ebenso, solange ein anderer die Compose-Sperre (`updater/compose.lock`) haelt. Der Neustart nimmt die Sperre ohne zu
warten: ein `up` des Updaters oder des Erststarts und ein `restart` ueberlappen nie, und ein gerade aktualisierter Container
wird nicht nachtraeglich noch einmal neu gestartet."""
import logging
import os
import time
from pathlib import Path

from smartheat_host import bundles, updater

logger = logging.getLogger("smartheat_host.waechter")

TAKT_SEKUNDEN = 30
UNGESUND_FOLGE = 3  # ungesunde Pruefungen in Folge bis zum Neustart
NEUSTARTS_JE_STUNDE = 3
STUNDE_SEKUNDEN = 3600
DIENST = "runtime"
PAUSE_PHASEN = ("installiere", "installiert", "probestart")


def pruefe(health: dict[str, str], zustand: dict, jetzt: float, pausiert: bool) -> tuple[str, dict]:
    """Eine Pruefung: (Aktion, neuer Zustand). `health` ist die Antwort von `ComposeRunner.health` (Dienst -> Health),
    `zustand` hat `unhealthy_folge`, `neustarts` (Zeitpunkte der letzten Stunde) und `aufgegeben` (schon gemeldet). Aktion:
    `nichts`, `neustart` (der Zeitpunkt steht schon in `neustarts`) oder `aufgegeben` (Grenze je Stunde erreicht)."""
    neustarts = [t for t in zustand.get("neustarts", []) if jetzt - t < STUNDE_SEKUNDEN]
    if pausiert or health.get(DIENST) != "unhealthy":
        # Weder pausiert noch ungesund: keine Beobachtung, die Folge (und die Meldung "aufgegeben") beginnt neu.
        return "nichts", {"unhealthy_folge": 0, "neustarts": neustarts, "aufgegeben": False}
    folge = zustand.get("unhealthy_folge", 0) + 1
    gemeldet = bool(zustand.get("aufgegeben"))
    if folge < UNGESUND_FOLGE:
        return "nichts", {"unhealthy_folge": folge, "neustarts": neustarts, "aufgegeben": gemeldet}
    if len(neustarts) >= NEUSTARTS_JE_STUNDE:
        if not gemeldet:
            logger.error("Laufzeit bleibt ungesund: %d Neustarts in der letzten Stunde, weiterer Neustart erst nach Ablauf "
                         "der Stunde", len(neustarts))
        return "aufgegeben", {"unhealthy_folge": folge, "neustarts": neustarts, "aufgegeben": True}
    return "neustart", {"unhealthy_folge": 0, "neustarts": [*neustarts, jetzt], "aufgegeben": False}


def pausiert(root: Path) -> bool:
    """Stellt der Updater gerade um, wechselt das System oder laeuft ein Werksreset? (Eine unlesbare Zustandsdatei gilt als
    leer, siehe `updater.State.load`.)"""
    if (root / "host" / "werksreset.json").exists():
        return True
    state = updater.State.load(root / "updater" / "state.json")
    return state.in_progress is not None or (state.system is not None and state.system["phase"] in PAUSE_PHASEN)


def schritt(root: Path, compose, zustand: dict, jetzt: float) -> dict:
    """Ein Takt: Zustandsdatei lesen, Health fragen, pruefen, bei `neustart` unter der Compose-Sperre neu starten.
    `compose` ist der `bundles.ComposeRunner` (in Tests eine Attrappe mit `sperre`, `health`, `restart`)."""
    state = updater.State.load(root / "updater" / "state.json")
    if pausiert(root) or state.current is None:
        return pruefe({}, zustand, jetzt, True)[1]
    aktion, neu = pruefe(compose.health(state.current), zustand, jetzt, False)
    if aktion != "neustart":
        return neu
    with compose.sperre(blockieren=False) as frei:
        # Unter der Sperre noch einmal lesen: der Updater schreibt seinen Auftrag, bevor er `up` aufruft.
        if not frei or pausiert(root):
            return pruefe({}, zustand, jetzt, True)[1]
        logger.warning("Laufzeit %s ist ungesund: Neustart von %s", state.current, DIENST)
        try:
            compose.restart(state.current, DIENST)
        except bundles.ComposeError as error:
            logger.warning("Neustart von %s gescheitert: %s", DIENST, error)  # zaehlt trotzdem zu den dreien je Stunde
    return neu


def main() -> None:  # pragma: no cover - systemd-Einstieg
    logging.basicConfig(level=logging.INFO)
    root = Path(os.environ.get("SHG_ROOT", "/var/lib/smartheat"))
    compose = bundles.ComposeRunner(root)
    zustand: dict = {"unhealthy_folge": 0, "neustarts": [], "aufgegeben": False}
    while True:
        try:
            zustand = schritt(root, compose, zustand, time.monotonic())
        except Exception:  # Fangnetz: ein unerwarteter Fehler beendet den Waechter nicht, der naechste Takt folgt
            logger.exception("Takt des Waechters mit unerwartetem Fehler abgebrochen")
        time.sleep(TAKT_SEKUNDEN)


if __name__ == "__main__":
    main()
