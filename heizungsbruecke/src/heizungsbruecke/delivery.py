"""Tick-Zustellung (Design-Spec 2026-09-26, Abschnitt 2), ersetzt failsafe.py.

Reine Zustandsmaschine ohne I/O: `step(state, event) -> (neuer_zustand, aktionen)`.
Der Regel-Worker (__main__) fuehrt die Aktionen aus und speist deren Ergebnisse
(`Published`, `ReadInvalid`, `EntitlementChecked`) als neue Ereignisse zurueck.

Ein Tick behaelt ueber alle Wiederholungen dieselbe `seq`: der Server beantwortet eine
bereits verarbeitete seq idempotent aus seinem gespeicherten Stand (tick.py,
handle_snapshot_v2), ein Retry erzeugt also nie einen doppelten Lernschritt. Abgelehnte
seqs speichert der Server nicht, ein Retry wird mit frischen Werten neu gerechnet.
"""
from dataclasses import dataclass, replace

from heizungsbruecke.entitlement import INACTIVE

ACK_TIMEOUT_SECONDS = 30
NOTBETRIEB_AFTER_SERVER_FAILURES = 2
# Wartezeit bis zum naechsten Versuch je Stufe; ab der letzten Stufe gilt deren Wert.
# Notbetrieb beginnt damit ~60-90 s nach dem ersten Publish, ein zurueckgekehrter
# Server wird spaetestens nach einer Stunde bemerkt.
SERVER_RETRY_DELAYS_SECONDS = (0, 300, 900, 3600)
DATA_RETRY_DELAYS_SECONDS = (30, 300, 900, 3600)

TRIGGERS = ("daily", "target_change")
STATUS_OK = "ok"
STATUS_SKIPPED_SUMMER = "skipped_summer"
STATUS_REJECTED = "rejected"
SOURCE_LOCAL = "local"
SOURCE_SERVER = "server"

NOTIFY_NOTBETRIEB_ON = "notbetrieb_on"
NOTIFY_NOTBETRIEB_OFF = "notbetrieb_off"
NOTIFY_DATENFEHLER_LOCAL = "datenfehler_local"
NOTIFY_DATENFEHLER_SERVER = "datenfehler_server"
NOTIFY_DATENFEHLER_RESOLVED = "datenfehler_resolved"

_NO_REASON = "ohne Begründung"

# Phase des offenen Ticks (nur im Speicher): sending -> awaiting_ack -> (querying_entitlement ->)
# waiting_retry -> sending ...
PHASE_SENDING = "sending"
PHASE_AWAITING_ACK = "awaiting_ack"
PHASE_QUERYING_ENTITLEMENT = "querying_entitlement"
PHASE_WAITING_RETRY = "waiting_retry"


@dataclass(frozen=True)
class PendingTick:
    seq: str
    trigger: str
    stage: int = 0
    phase: str = PHASE_SENDING
    # Jeder geplante ack_timeout/retry_due traegt die Generation, bei der er geplant wurde; nur
    # die aktuelle zaehlt. Sonst erzeugte eine verspaetete Antwort doppelte Versuche.
    gen: int = 0


@dataclass(frozen=True)
class DataFault:
    source: str  # SOURCE_LOCAL | SOURCE_SERVER
    detail: tuple[str, ...]  # lokal: betroffene Rollen (sortiert); Server: (Ablehnungsgrund,)

    def key(self) -> tuple:
        """Identitaet der Stoerung fuer "gleicher Fehler wie zuletzt?". Der Server-Grund
        traegt bei R4 den Messwert ("unplausibler Wert für dat: 99 (erlaubt ...)"); ein
        driftender Wert ist dieselbe Stoerung, zaehlt also nur der Teil vor dem ersten
        Doppelpunkt."""
        if self.source == SOURCE_SERVER:
            return (self.source, self.detail[0].split(":", 1)[0] if self.detail else "")
        return (self.source, self.detail)


@dataclass(frozen=True)
class DeliveryState:
    pending: PendingTick | None = None
    server_failures: int = 0  # Ack-Timeouts in Folge, ueber Ticks hinweg
    notbetrieb: bool = False  # Server schweigt; macht den Notfall-Boost scharf
    datenfehler: DataFault | None = None


# --- Ereignisse ---

@dataclass(frozen=True)
class Boot:
    pass


@dataclass(frozen=True)
class TickDue:
    seq: str
    trigger: str


@dataclass(frozen=True)
class ReadInvalid:
    seq: str
    roles: tuple[str, ...]


@dataclass(frozen=True)
class Published:
    seq: str


@dataclass(frozen=True)
class Ack:
    seq: str
    status: str
    reason: str | None = None


@dataclass(frozen=True)
class AckTimeout:
    seq: str
    gen: int


@dataclass(frozen=True)
class RetryDue:
    seq: str
    gen: int


@dataclass(frozen=True)
class EntitlementChecked:
    seq: str
    status: str


# --- Aktionen ---

@dataclass(frozen=True)
class Attempt:
    seq: str
    trigger: str


@dataclass(frozen=True)
class ScheduleAckTimeout:
    seq: str
    gen: int
    delay_s: float


@dataclass(frozen=True)
class ScheduleRetry:
    seq: str
    gen: int
    delay_s: float


@dataclass(frozen=True)
class QueryEntitlement:
    seq: str


@dataclass(frozen=True)
class EnterAboInactive:
    pass


@dataclass(frozen=True)
class Notify:
    kind: str
    detail: tuple[str, ...] = ()


@dataclass(frozen=True)
class PublishFailsafe:
    active: bool


@dataclass(frozen=True)
class EndEmergencyBoost:
    pass


def accepts_ack(state: DeliveryState, seq) -> bool:
    """Eine Antwort zaehlt nur fuer den offenen Tick -- auch verspaetet, waehrend schon
    auf einen Retry gewartet wird."""
    return state.pending is not None and state.pending.seq == seq


def step(state: DeliveryState, event) -> tuple[DeliveryState, list]:
    if isinstance(event, Boot):
        return _boot(state)
    if isinstance(event, TickDue):
        return _tick_due(state, event)
    if isinstance(event, ReadInvalid):
        return _read_invalid(state, event)
    if isinstance(event, Published):
        return _published(state, event)
    if isinstance(event, Ack):
        return _ack(state, event)
    if isinstance(event, AckTimeout):
        return _ack_timeout(state, event)
    if isinstance(event, RetryDue):
        return _retry_due(state, event)
    if isinstance(event, EntitlementChecked):
        return _entitlement_checked(state, event)
    raise TypeError(f"Unbekanntes Zustell-Ereignis: {event!r}")


def _is_current(pending: PendingTick | None, seq: str, gen: int) -> bool:
    return pending is not None and pending.seq == seq and pending.gen == gen


def _retry(state: DeliveryState, delays: tuple[int, ...]) -> tuple[DeliveryState, ScheduleRetry]:
    """Plant den naechsten Versuch mit der Wartezeit der aktuellen Stufe, danach stage+1."""
    pending = state.pending
    gen = pending.gen + 1
    delay = delays[min(pending.stage, len(delays) - 1)]
    new_pending = replace(pending, stage=pending.stage + 1, phase=PHASE_WAITING_RETRY, gen=gen)
    return replace(state, pending=new_pending), ScheduleRetry(pending.seq, gen, delay)


def _boot(state):
    if state.pending is None:
        return state, []
    pending = PendingTick(seq=state.pending.seq, trigger=state.pending.trigger)
    return replace(state, pending=pending), [Attempt(pending.seq, pending.trigger)]


def _tick_due(state, event):
    # Ersetzt einen offenen Tick; dessen geplante Retries/Timeouts laufen ueber die
    # seq-Pruefung ins Leere.
    return replace(state, pending=PendingTick(seq=event.seq, trigger=event.trigger)), [
        Attempt(event.seq, event.trigger),
    ]


def _read_invalid(state, event):
    if state.pending is None or state.pending.seq != event.seq:
        return state, []
    fault = DataFault(SOURCE_LOCAL, tuple(sorted(event.roles)))
    actions = [] if fault == state.datenfehler else [Notify(NOTIFY_DATENFEHLER_LOCAL, fault.detail)]
    new_state, retry = _retry(replace(state, datenfehler=fault), DATA_RETRY_DELAYS_SECONDS)
    return new_state, actions + [retry]


def _published(state, event):
    pending = state.pending
    if pending is None or pending.seq != event.seq:
        return state, []
    gen = pending.gen + 1
    new_pending = replace(pending, phase=PHASE_AWAITING_ACK, gen=gen)
    return replace(state, pending=new_pending), [ScheduleAckTimeout(pending.seq, gen, ACK_TIMEOUT_SECONDS)]


def _notbetrieb_end(state) -> list:
    if not state.notbetrieb:
        return []
    return [PublishFailsafe(False), EndEmergencyBoost(), Notify(NOTIFY_NOTBETRIEB_OFF)]


def _answered_with_fault(state, fault: DataFault, notify_kind: str):
    """Antwort ohne neue Werte: der Server lebt (Zaehler zurueck, Notbetrieb endet), der
    Datenfehler wird gemeldet, wenn er neu ist. Bei gleicher Stoerung bleibt die erste
    Begruendung, damit weder eine Meldung noch ein Schreiben von failsafe_state.json folgt.
    Einen Retry plant nur die Antwort auf den laufenden Versuch (awaiting_ack); eine doppelte
    oder verspaetete Antwort laesst den geplanten Retry bzw. die Abo-Abfrage unveraendert,
    sonst uebersprange sie Stufen."""
    actions = _notbetrieb_end(state)
    if state.datenfehler is not None and fault.key() == state.datenfehler.key():
        fault = state.datenfehler
    else:
        actions.append(Notify(notify_kind, fault.detail))
    answered = replace(state, server_failures=0, notbetrieb=False, datenfehler=fault)
    if state.pending.phase != PHASE_AWAITING_ACK:
        return answered, actions
    new_state, retry = _retry(answered, DATA_RETRY_DELAYS_SECONDS)
    return new_state, actions + [retry]


def _ack(state, event):
    if not accepts_ack(state, event.seq):
        return state, []
    if event.status == STATUS_REJECTED:
        fault = DataFault(SOURCE_SERVER, (event.reason or _NO_REASON,))
        return _answered_with_fault(state, fault, NOTIFY_DATENFEHLER_SERVER)
    actions = _notbetrieb_end(state)
    if state.datenfehler is not None:
        actions.append(Notify(NOTIFY_DATENFEHLER_RESOLVED))
    return replace(state, pending=None, server_failures=0, notbetrieb=False, datenfehler=None), actions


def _ack_timeout(state, event):
    pending = state.pending
    if not _is_current(pending, event.seq, event.gen) or pending.phase != PHASE_AWAITING_ACK:
        return state, []
    state = replace(state, server_failures=state.server_failures + 1)
    if state.server_failures >= NOTBETRIEB_AFTER_SERVER_FAILURES and not state.notbetrieb:
        # Erst klaeren, ob das Abo inaktiv ist (dann Abo-inaktiv-Modus statt Alarm).
        querying = replace(pending, phase=PHASE_QUERYING_ENTITLEMENT)
        return replace(state, pending=querying), [QueryEntitlement(pending.seq)]
    new_state, retry = _retry(state, SERVER_RETRY_DELAYS_SECONDS)
    return new_state, [retry]


def _retry_due(state, event):
    pending = state.pending
    if not _is_current(pending, event.seq, event.gen) or pending.phase != PHASE_WAITING_RETRY:
        return state, []
    return replace(state, pending=replace(pending, phase=PHASE_SENDING)), [Attempt(pending.seq, pending.trigger)]


def _entitlement_checked(state, event):
    pending = state.pending
    if pending is None or pending.seq != event.seq or pending.phase != PHASE_QUERYING_ENTITLEMENT:
        return state, []
    if state.server_failures < NOTBETRIEB_AFTER_SERVER_FAILURES:
        # Waehrend der Abfrage kam eine Antwort ohne Werte: der Server lebt, das Abo-Ergebnis
        # zaehlt nicht mehr.
        new_state, retry = _retry(state, DATA_RETRY_DELAYS_SECONDS)
        return new_state, [retry]
    if event.status == INACTIVE:
        return replace(state, pending=None, notbetrieb=True), [EnterAboInactive()]
    new_state, retry = _retry(replace(state, notbetrieb=True), SERVER_RETRY_DELAYS_SECONDS)
    return new_state, [PublishFailsafe(True), Notify(NOTIFY_NOTBETRIEB_ON), retry]


def to_persisted(state: DeliveryState) -> dict:
    """Inhalt von failsafe_state.json. stage/phase/gen/server_failures sind
    fluechtig -- der Aufrufer schreibt nur, wenn sich dieses Dict aendert."""
    fault, pending = state.datenfehler, state.pending
    return {
        "failsafe_active": state.notbetrieb,
        "datenfehler": None if fault is None else {"source": fault.source, "detail": list(fault.detail)},
        "pending": None if pending is None else {"seq": pending.seq, "trigger": pending.trigger},
    }


def from_persisted(raw) -> DeliveryState:
    """Tolerant: fehlende oder kaputte Felder werden zu Standardwerten (alte Datei mit nur
    failsafe_active, SD-Karten-Muell). Ein Lesefehler darf den Start nie verhindern."""
    if not isinstance(raw, dict):
        return DeliveryState()
    return DeliveryState(
        pending=_parse_pending(raw.get("pending")),
        notbetrieb=raw.get("failsafe_active") is True,
        datenfehler=_parse_fault(raw.get("datenfehler")),
    )


def _parse_pending(raw) -> PendingTick | None:
    if not isinstance(raw, dict):
        return None
    seq, trigger = raw.get("seq"), raw.get("trigger")
    if not isinstance(seq, str) or not seq or not isinstance(trigger, str) or trigger not in TRIGGERS:
        return None
    return PendingTick(seq=seq, trigger=trigger)


def _parse_fault(raw) -> DataFault | None:
    if not isinstance(raw, dict):
        return None
    source, detail = raw.get("source"), raw.get("detail")
    if source not in (SOURCE_LOCAL, SOURCE_SERVER):
        return None
    if not isinstance(detail, list) or not all(isinstance(item, str) for item in detail):
        return None
    return DataFault(source=source, detail=tuple(detail))


def notification_text(kind: str, detail: tuple[str, ...], entity_ids: dict[str, str]) -> str:
    if kind == NOTIFY_NOTBETRIEB_ON:
        return "Heizungsbrücke: Server antwortet nicht, Notbetrieb aktiv. Die Heizung wird bei Bedarf lokal abgesichert."
    if kind == NOTIFY_NOTBETRIEB_OFF:
        return "Heizungsbrücke: Serververbindung wiederhergestellt, Notbetrieb beendet."
    if kind == NOTIFY_DATENFEHLER_LOCAL:
        sensors = ", ".join(f"{role} ({entity_ids.get(role, 'nicht zugeordnet')})" for role in detail)
        return (
            f"Heizungsbrücke: Sensor(en) ohne gültigen Wert: {sensors}. Die Heizkurve bleibt "
            f"unverändert, bis die Werte wieder verfügbar sind (z. B. Batterie prüfen)."
        )
    if kind == NOTIFY_DATENFEHLER_SERVER:
        reason = detail[0] if detail else _NO_REASON
        return f"Heizungsbrücke: Server hat die Messwerte abgelehnt ({reason}). Die Heizkurve bleibt unverändert."
    if kind == NOTIFY_DATENFEHLER_RESOLVED:
        return "Heizungsbrücke: Messwerte wieder gültig, Heizkurve wird wieder angepasst."
    raise ValueError(f"Unbekannte Meldungsart: {kind!r}")


def build_discovery_config(tenant_id: str) -> dict:
    """Builds the MQTT Discovery config payload for the fail-safe binary_sensor. HA's
    MQTT integration creates the entity from this automatically -- no configuration.yaml
    needed on the customer side.
    """
    return {
        "name": "Fail-Safe",
        "unique_id": f"heizungsbruecke_{tenant_id}_failsafe",
        "state_topic": f"smartheat/{tenant_id}/status/failsafe",
        "availability_topic": f"smartheat/{tenant_id}/status/availability",
        "payload_on": "ON",
        "payload_off": "OFF",
        "device_class": "problem",
        "device": {
            "identifiers": [f"heizungsbruecke_{tenant_id}"],
            "name": f"Heizungsbruecke ({tenant_id})",
            "manufacturer": "SmartHeat",
        },
    }


def build_state_payload(active: bool) -> str:
    return "ON" if active else "OFF"
