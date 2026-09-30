"""Erkennung "Therme liefert keine Waerme" (TP12f, Spec 1). Reine Logik ohne I/O.

Der Systemregler fordert Waerme an (Vorlauf-Soll > 0), aber der gemessene Vorlauf steigt nicht ueber die
Raumtemperatur. Massgeblich ist der Anteil der angeforderten Uebertemperatur, den der Vorlauf erreicht:
(Vorlauf - Raum) / (Soll - Raum). Konstanten: Kalibrierung 2026-09-30 (Spec 5.2), Positivseite noch offen."""
import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

# Laengere Unterbrechung der Anforderung beendet die Phase (kuerzere, z. B. eine Warmwasserladung, nicht).
REQUEST_PAUSE_TOLERANCE = timedelta(minutes=45)
# Nach Phasenbeginn und nach jeder Unterbrechung nicht bewerten: Restwaerme im Vorlauf.
SETTLE = timedelta(minutes=30)
# So lange muss die Beobachtung mindestens laufen, bevor "Waerme fehlt" gilt.
MIN_REQUEST = timedelta(hours=3)
# Ein gesetztes Flag faellt erst, wenn bewertete Ticks so lange am Stueck Anteil >= SHARE_THRESHOLD haben:
# Restwaerme nach einer Warmwasserladung bzw. eine Ladung, die zwischen zwei Cloud-Abfragen versteckt bleibt,
# zeigt sich als kurzer Ausschlag. Halte-Zeit statt Tick-Zahl, weil die 5-min-Ticks den zuletzt abgefragten
# Wert wiederholen (zwei Ticks in Folge sind oft dieselbe Abfrage).
CLEAR_HOLD = timedelta(minutes=60)
SHARE_THRESHOLD = 0.5
# Mindest-Uebertemperatur (Soll - Raum); gleich SHARE_MIN_LIFT_K im Server (samples.py).
MIN_LIFT = 5.0


@dataclass(frozen=True)
class WaermeState:
    """fehlt_seit ist das Flag; alles andere ist Phasenzustand (nur zur Laufzeit, Spec 1.4)."""
    fehlt_seit: datetime | None = None
    beobachtung_seit: datetime | None = None
    settle_bis: datetime | None = None
    unter_schwelle: bool = False
    letzte_anforderung: datetime | None = None
    unterbrochen: bool = False
    klar_seit: datetime | None = None


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def share(setpoint, flow, room) -> float | None:
    """Anteil der angeforderten Uebertemperatur, den der Vorlauf erreicht; None ohne brauchbare Werte
    oder bei weniger als MIN_LIFT angeforderter Uebertemperatur."""
    if not (_finite(setpoint) and _finite(flow) and _finite(room)):
        return None
    lift = setpoint - room
    if lift < MIN_LIFT:
        return None
    return (flow - room) / lift


def evaluate(state: WaermeState, now: datetime, setpoint, flow, room) -> WaermeState:
    """Neuer Zustand nach einem Telemetrie-Tick. Ticks ohne Aussage (fehlende Werte, zu geringe
    Anforderung, SETTLE) aendern das Flag nie: kein Setzen und kein Loeschen aus Unwissen. Geloescht wird ein
    gesetztes Flag erst nach CLEAR_HOLD am Stueck mit Anteil >= SHARE_THRESHOLD (klar_seit)."""
    if not _finite(setpoint):
        return state
    if setpoint <= 0:
        # Unterbrechung der Anforderung; ohne laufende Phase gibt es nichts zu unterbrechen.
        return state if state.letzte_anforderung is None else replace(state, unterbrochen=True)
    last = state.letzte_anforderung
    if last is None or state.beobachtung_seit is None or now - last > REQUEST_PAUSE_TOLERANCE:
        state = replace(
            state, beobachtung_seit=now, settle_bis=now + SETTLE, unter_schwelle=False, unterbrochen=False,
            klar_seit=None,
        )
    elif state.unterbrochen:
        state = replace(state, settle_bis=now + SETTLE, unterbrochen=False, klar_seit=None)
    state = replace(state, letzte_anforderung=now)
    if state.settle_bis is not None and now < state.settle_bis:
        return state
    value = share(setpoint, flow, room)
    if value is None:
        return state
    if value >= SHARE_THRESHOLD:
        if state.fehlt_seit is None:
            return replace(state, beobachtung_seit=now, unter_schwelle=False)
        klar_seit = state.klar_seit or now
        if now - klar_seit >= CLEAR_HOLD:
            return replace(state, fehlt_seit=None, beobachtung_seit=now, unter_schwelle=False, klar_seit=None)
        return replace(state, klar_seit=klar_seit)
    state = replace(state, unter_schwelle=True, klar_seit=None)
    if state.fehlt_seit is None and state.beobachtung_seit is not None and now - state.beobachtung_seit >= MIN_REQUEST:
        state = replace(state, fehlt_seit=now)
    return state


def parse_since(value) -> datetime | None:
    """ISO-Zeitpunkt mit Zeitzone aus backup.json; alles andere gilt als "kein Flag"."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
