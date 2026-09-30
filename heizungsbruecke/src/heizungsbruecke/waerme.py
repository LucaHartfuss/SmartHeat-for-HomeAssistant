"""Erkennung "Therme liefert keine Waerme" (TP12f, Spec 1). Reine Logik ohne I/O.

Der Systemregler fordert Waerme an (Vorlauf-Soll > 0), aber der gemessene Vorlauf steigt nicht ueber die
Raumtemperatur. Massgeblich ist der Anteil der angeforderten Uebertemperatur, den der Vorlauf erreicht:
(Vorlauf - Raum) / (Soll - Raum). Konstanten: Kalibrierung 2026-09-30 (Spec 5.2), Positivseite noch offen."""
import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from statistics import median

# Laengere Unterbrechung der Anforderung beendet die Phase (kuerzere, z. B. eine Warmwasserladung, nicht).
REQUEST_PAUSE_TOLERANCE = timedelta(minutes=45)
# Nach Phasenbeginn und nach jeder Unterbrechung nicht bewerten: Restwaerme im Vorlauf.
SETTLE = timedelta(minutes=30)
# So lange muss die Beobachtung mindestens laufen, bevor "Waerme fehlt" gilt.
MIN_REQUEST = timedelta(hours=3)
# Ein gesetztes Flag faellt erst, wenn die bewerteten Anteile der letzten CLEAR_WINDOW (voll abgedeckt) im Median
# mindestens SHARE_THRESHOLD erreichen: Restwaerme nach einer Warmwasserladung bzw. eine Ladung zwischen zwei
# Cloud-Abfragen ist ein kurzer Ausschlag, ein taktender, aber heizender Brenner liefert dagegen mehrheitlich gute
# Werte. Fenster statt Tick-Zahl, weil die 5-min-Ticks den zuletzt abgefragten Wert wiederholen (~30 min je Abfrage).
CLEAR_WINDOW = timedelta(minutes=60)
# Der Anker deckt nur Zeitstempel-Jitter ab, nicht Luecken ohne Aussage: Der aelteste Wert im Fenster (ohne Anker) muss
# mindestens CLEAR_WINDOW - ANCHOR_TOLERANCE alt sein. Sonst koennte nach langer Aussagelosigkeit (Hub unter MIN_LIFT,
# Sensor fehlt: die Phase laeuft weiter) ein einzelner guter Wert ueber den Anker entwarnen. Das garantiert mindestens zwei
# Werte im Fenster; bei Tick-Abstand <= ANCHOR_TOLERANCE (hier 5 min) liegen Werte auch am alten Rand dicht genug.
# Luecken ohne Aussage am alten Rand des Fensters oder ueber das ganze Fenster koennen das Flag damit nicht loeschen; eine
# Luecke in der Mitte des Fensters mit je einem Wert an beiden Enden schon (bekannte Grenze, zurueckgestellt: Mindestzahl
# Werte im Fenster, z. B. len(inside) >= 6, nach der Pruefung der Positivseite neu bewerten).
ANCHOR_TOLERANCE = timedelta(minutes=10)
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
    # Bewertete (Zeitpunkt, Anteil) der letzten CLEAR_WINDOW plus ein Anker, nur zur Laufzeit; unveraenderlich.
    anteile: tuple[tuple[datetime, float], ...] = ()


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
    gesetztes Flag erst, wenn ein bewerteter Tick >= SHARE_THRESHOLD kommt, das Fenster der letzten CLEAR_WINDOW
    voll abgedeckt ist (aeltester Wert, auch der Anker, mindestens CLEAR_WINDOW alt, der aelteste Wert im Fenster
    mindestens CLEAR_WINDOW - ANCHOR_TOLERANCE) und der Median der Anteile im Fenster >= SHARE_THRESHOLD ist."""
    if not _finite(setpoint):
        return state
    if setpoint <= 0:
        # Unterbrechung der Anforderung; ohne laufende Phase gibt es nichts zu unterbrechen.
        return state if state.letzte_anforderung is None else replace(state, unterbrochen=True)
    last = state.letzte_anforderung
    if last is None or state.beobachtung_seit is None or now - last > REQUEST_PAUSE_TOLERANCE:
        state = replace(
            state, beobachtung_seit=now, settle_bis=now + SETTLE, unter_schwelle=False, unterbrochen=False,
            anteile=(),
        )
    elif state.unterbrochen:
        state = replace(state, settle_bis=now + SETTLE, unterbrochen=False, anteile=())
    state = replace(state, letzte_anforderung=now)
    if state.settle_bis is not None and now < state.settle_bis:
        return state
    value = share(setpoint, flow, room)
    if value is None:
        return state
    # Fenster: alle Werte bis CLEAR_WINDOW alt, dazu hoechstens ein Anker (der neueste aeltere Wert), der nur die
    # Abdeckung belegt, nicht in den Median eingeht. Ohne Anker waere die Abdeckung nur bei Tick-Abstand genau 5 min
    # erfuellbar: die echten Zeitpunkte streuen (Scheduler-Latenz), ein Wert ist selten genau CLEAR_WINDOW alt.
    items = state.anteile + ((now, value),)
    inside = tuple(item for item in items if now - item[0] <= CLEAR_WINDOW)
    older = tuple(item for item in items if now - item[0] > CLEAR_WINDOW)
    kept = older[-1:] + inside
    state = replace(state, anteile=kept)
    if value >= SHARE_THRESHOLD:
        if state.fehlt_seit is None:
            return replace(state, beobachtung_seit=now, unter_schwelle=False)
        covered = now - kept[0][0] >= CLEAR_WINDOW and now - inside[0][0] >= CLEAR_WINDOW - ANCHOR_TOLERANCE
        if covered and median(anteil for _, anteil in inside) >= SHARE_THRESHOLD:
            return replace(state, fehlt_seit=None, beobachtung_seit=now, unter_schwelle=False, anteile=())
        return state
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
