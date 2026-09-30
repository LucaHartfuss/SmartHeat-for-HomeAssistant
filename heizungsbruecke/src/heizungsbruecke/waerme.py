"""Erkennung "Therme liefert keine Waerme" (TP12f, Spec 1). Reine Logik ohne I/O.

Der Systemregler fordert Waerme an (Vorlauf-Soll > 0), aber der gemessene Vorlauf steigt nicht ueber die
Raumtemperatur. Massgeblich ist der Anteil der angeforderten Uebertemperatur, den der Vorlauf erreicht:
(Vorlauf - Raum) / (Soll - Raum). Konstanten sind vorlaeufig und werden vor dem Release kalibriert."""
import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

# Laengere Unterbrechung der Anforderung beendet die Phase (kuerzere, z. B. eine Warmwasserladung, nicht).
REQUEST_PAUSE_TOLERANCE = timedelta(minutes=45)
# Nach Phasenbeginn und nach jeder Unterbrechung nicht bewerten: Restwaerme im Vorlauf.
SETTLE = timedelta(minutes=30)
# So lange muss die Beobachtung mindestens laufen, bevor "Waerme fehlt" gilt.
MIN_REQUEST = timedelta(hours=3)
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
    Anforderung, SETTLE) aendern das Flag nie: kein Setzen und kein Loeschen aus Unwissen."""
    if not _finite(setpoint):
        return state
    if setpoint <= 0:
        # Unterbrechung der Anforderung; ohne laufende Phase gibt es nichts zu unterbrechen.
        return state if state.letzte_anforderung is None else replace(state, unterbrochen=True)
    last = state.letzte_anforderung
    if last is None or state.beobachtung_seit is None or now - last > REQUEST_PAUSE_TOLERANCE:
        state = replace(
            state, beobachtung_seit=now, settle_bis=now + SETTLE, unter_schwelle=False, unterbrochen=False,
        )
    elif state.unterbrochen:
        state = replace(state, settle_bis=now + SETTLE, unterbrochen=False)
    state = replace(state, letzte_anforderung=now)
    if state.settle_bis is not None and now < state.settle_bis:
        return state
    value = share(setpoint, flow, room)
    if value is None:
        return state
    if value >= SHARE_THRESHOLD:
        return replace(state, fehlt_seit=None, beobachtung_seit=now, unter_schwelle=False)
    state = replace(state, unter_schwelle=True)
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
