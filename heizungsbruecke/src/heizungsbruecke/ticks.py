"""Tick-Zustellung ausfuehren: die Aktionen der Zustandsmaschine `delivery` (Versuch,
Abo-Abfrage, Zeitplan, Meldungen) und die Server-Antworten."""
import logging
import math
import uuid
from typing import TypeGuard

from heizungsbruecke import abo
from heizungsbruecke.notifier import STATE_OK
from heizungsbruecke.runtime import EV_ACK_TIMEOUT, EV_RETRY_DUE, Runtime
from heizungsbruecke.snapshot import SNAPSHOT_SCHEMA_VERSION, publish_snapshot, read_snapshot
from smartheat_core import wallclock
from smartheat_core.pipeline import DeviceWriteError
from smartheat_runtime import delivery, entitlement
from smartheat_runtime.state import StorageError
from smartheat_runtime.worker import Event

logger = logging.getLogger(__name__)

_SETPOINT_STATUSES_WITH_VALUES = (delivery.STATUS_OK, delivery.STATUS_SKIPPED)


def _is_finite_number(value) -> TypeGuard[float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


_LEARNED_KEYS = frozenset({"curve", "heat_limit"})


def _store_learned(rt: Runtime, learned) -> None:
    """Lernwerte der Antwort fuer das Statusereignis (Plan 3c). Nur Anzeige: ungueltige Werte werden geloggt und
    ignoriert, ein Speicherfehler bricht die Antwort nicht ab (der Regelpfad ist schon durch)."""
    if not (
        isinstance(learned, dict) and learned and set(learned) <= _LEARNED_KEYS
        and all(_is_finite_number(value) for value in learned.values())
    ):
        logger.info("Serverantwort ohne gueltige Lernwerte (%r), Anzeige unveraendert", learned)
        return
    try:
        rt.store.update(learned=dict(learned))
    except StorageError as error:
        logger.warning("Lernwerte nicht gespeichert: %s", error)


def start_tick(rt: Runtime, trigger: str) -> None:
    deliver(rt, delivery.TickDue(seq=str(uuid.uuid4()), trigger=trigger))


# Pruef-Tick (TP12b, Spec 1): der Server lernt nur auf "daily"; ein target_change bei unveraendertem
# Soll ergibt Vorsteuerung null und dieselben Werte.
PROBE_TRIGGER = "target_change"


def start_probe_tick(rt: Runtime, reason: str) -> None:
    """Klaert den Zustand des Servers ohne fachlichen Anlass (Verbindungsverlust, Notbetrieb ohne
    offenen Tick). Laeuft durch die normale Zustellung; bucht last_published_target_rt nicht um,
    weil er kein Soll-Wechsel ist."""
    logger.warning("Pruef-Tick: %s", reason)
    start_tick(rt, PROBE_TRIGGER)


def deliver(rt: Runtime, event) -> None:
    """Fuehrt ein Ereignis durch delivery.step und dessen Aktionen aus. Aktionen mit Ergebnis
    (Versuch, Abo-Abfrage) speisen es als neues Ereignis zurueck."""
    pending_events = [event]
    while pending_events:
        current = pending_events.pop(0)
        after, actions = delivery.step(rt.store.state.delivery, current)
        rt.store.set_delivery(after)
        for action in actions:
            try:
                follow_up = _execute(rt, action)
            except Exception:
                logger.exception("Zustell-Aktion %r fehlgeschlagen", action)
                follow_up = _fallback_follow_up(action)
            if follow_up is not None:
                pending_events.append(follow_up)


def _fallback_follow_up(action):
    """Ohne Rueckmeldung bliebe der offene Tick ohne Zeitplaneintrag haengen: ein gescheiterter
    Versuch zaehlt wie ein Publish ohne Antwort, eine gescheiterte Abo-Abfrage wie
    "unbekannt" (fail-open)."""
    if isinstance(action, delivery.Attempt):
        return delivery.Published(seq=action.seq)
    if isinstance(action, delivery.QueryEntitlement):
        return delivery.EntitlementChecked(seq=action.seq, status=entitlement.UNKNOWN)
    return None


def _execute(rt: Runtime, action):
    if isinstance(action, delivery.Attempt):
        return _attempt(rt, action.seq, action.trigger)
    if isinstance(action, delivery.QueryEntitlement):
        status = entitlement.query(rt.config)
        return delivery.EntitlementChecked(seq=action.seq, status=status)
    if isinstance(action, delivery.ScheduleAckTimeout):
        rt.worker.schedule(action.delay_s, Event(EV_ACK_TIMEOUT, {"seq": action.seq, "gen": action.gen}))
    elif isinstance(action, delivery.ScheduleRetry):
        rt.worker.schedule(action.delay_s, Event(EV_RETRY_DUE, {"seq": action.seq, "gen": action.gen}))
    elif isinstance(action, delivery.EnterAboInactive):
        abo.enter_inactive(rt, wallclock.now())
    elif isinstance(action, delivery.Notify):
        _notify(rt, action)
    elif isinstance(action, delivery.EndEmergencyBoost):
        rt.override.set_boosts(comfort=rt.store.state.boost_active, emergency=False)
    else:
        raise TypeError(f"Unbekannte Zustell-Aktion: {action!r}")
    return None


def _attempt(rt: Runtime, seq: str, trigger: str):
    """Hebel, Raum-Soll und room_actual frisch lesen; bei ungueltigem Wert kein Publish
    (ReadInvalid). Auch ein Publish-Fehler meldet Published: der Ack-Timeout plant dann den
    naechsten Versuch, die Retry-Kette reisst nie ab."""
    # Kurz nach einem eigenen Schreiben (erster Start: _prime schreibt die Startverschiebung, der
    # erzwungene Tick folgt Sekunden spaeter; eine Soll-Aenderung kurz vor dem Tagestick) zeigt HA bei
    # mypyllant noch die alten Werte; der Server protokolliert beim Erstkontakt die gemeldeten Werte und
    # vergleicht sie an jedem Tagestick mit seinen zuletzt gesendeten ("Anlage folgt nicht"). Wie
    # derived.sync: bis LeverPipeline.settled gilt fuer jeden geschriebenen Hebel der eigene letzte Schreibwert
    # (Audit 3, A3-02: auch Steigung und Heizgrenze, nicht nur die Parallelverschiebung). Der vorbereitete Hebel
    # (Vaillant: Wunschtemperatur der Zone) faellt sonst bei ruhender Zone auf den Wiederherstellungspunkt zurueck.
    pipeline = rt.override
    binding = pipeline.binding
    known: dict[str, float | None] = {}
    for lever in binding.description.lever_set.levers:
        last = pipeline.last_written(lever)
        if not pipeline.settled(lever) and last is not None:
            known[lever] = last
        elif lever == binding.description.prepared_lever:
            known[lever] = binding.read_or(lever, rt.store.state.restore_point.get(lever))
    read = read_snapshot(rt.manifest, rt.signals, binding, known)
    if read.invalid:
        logger.warning("Snapshot (seq=%s) zurueckgehalten, ungueltige Werte: %s", seq, ", ".join(read.invalid))
        return delivery.ReadInvalid(seq=seq, roles=read.invalid)
    assert read.room_target is not None  # sonst stuende room_target in read.invalid
    if rt.mqtt_client is None or not rt.mqtt_client.is_connected():
        # Ohne Verbindung nicht publizieren: paho wuerde QoS-1-Nachrichten stauen und nach einem
        # langen Ausfall einen Stunden alten Messwertsatz nachliefern. Das (Wieder-)Verbinden
        # startet den Versuch sofort neu (N3), sonst plant der Ack-Timeout den naechsten.
        logger.warning("Snapshot (seq=%s) nicht gesendet, keine MQTT-Verbindung", seq)
        return delivery.Published(seq=seq, unsent=True)
    # Durchsetzungs-KPI: der beim ersten erfolgreichen Publish dieser seq gepinnte Eintrag reist bei jedem
    # Retry unveraendert mit; ein zwischenzeitlich neu erkannter Eingriff wartet auf die naechste
    # seq (siehe Runtime.manual_override_seq).
    if rt.manual_override_seq == seq:
        pending_override = rt.manual_override_sent
    else:
        pending_override = rt.store.state.manual_override_pending
    try:
        publish_snapshot(
            rt.mqtt_client, seq=seq, trigger=trigger, room_target=read.room_target, levers=read.levers,
            manual_override=pending_override, readonly=read.readonly,
        )
        rt.manual_override_sent = pending_override
        rt.manual_override_seq = seq
        logger.info("Voller Snapshot veroeffentlicht (seq=%s, trigger=%s)", seq, trigger)
    except Exception:
        logger.exception("Snapshot (seq=%s) konnte nicht veroeffentlicht werden - Retry nach dem Ack-Timeout", seq)
    return delivery.Published(seq=seq)


# Praefix je Stoerungsquelle fuer den Meldezustand "datenfehler"; abgeleitet aus
# delivery.DataFault.key(), nicht neu kodiert.
_FAULT_PREFIXES = {
    delivery.SOURCE_LOCAL: "lokal",
    delivery.SOURCE_SERVER: "server",
    delivery.SOURCE_WRITE: "anlage",
}


def _fault_state(source: str, detail: tuple[str, ...]) -> str:
    """Zustand des Meldeschluessels "datenfehler" mit derselben Stoerungsidentitaet wie
    delivery.DataFault.key(): eine andere Stoerung wird gemeldet, ein driftender Server-Messwert
    nicht."""
    key = delivery.DataFault(source, detail).key()
    prefix = _FAULT_PREFIXES[key[0]]
    if key[0] == delivery.SOURCE_WRITE:
        return prefix
    rest = key[1]
    if isinstance(rest, tuple):
        rest = ",".join(rest)
    return f"{prefix}:{rest}"


def _notice(kind: str, detail: tuple[str, ...]) -> tuple[str, str]:
    """(Meldeschluessel, Zustand) je Meldungsart der Zustellung."""
    if kind == delivery.NOTIFY_NOTBETRIEB_ON:
        return "notbetrieb", "aktiv"
    if kind == delivery.NOTIFY_NOTBETRIEB_OFF:
        return "notbetrieb", STATE_OK
    if kind == delivery.NOTIFY_DATENFEHLER_LOCAL:
        return "datenfehler", _fault_state(delivery.SOURCE_LOCAL, detail)
    if kind == delivery.NOTIFY_DATENFEHLER_SERVER:
        return "datenfehler", _fault_state(delivery.SOURCE_SERVER, detail)
    if kind == delivery.NOTIFY_DATENFEHLER_WRITE:
        return "datenfehler", _fault_state(delivery.SOURCE_WRITE, detail)
    if kind in (delivery.NOTIFY_DATENFEHLER_RESOLVED, delivery.NOTIFY_WRITE_RESOLVED):
        return "datenfehler", STATE_OK
    raise ValueError(f"Unbekannte Meldungsart: {kind!r}")


def seed_notices(notifier, delivery_state) -> None:
    """Notbetrieb und Datenfehler aus failsafe_state.json, die im Meldezustand fehlen (Update von
    0.18.0, gescheitertes Schreiben von backup.json), still uebernehmen: sonst bliebe die
    Entwarnung nach dem Neustart aus."""
    if delivery_state.notbetrieb:
        notifier.seed("notbetrieb", "aktiv")
    fault = delivery_state.datenfehler
    if fault is not None and not delivery.is_storage_fault(fault):
        notifier.seed("datenfehler", _fault_state(fault.source, fault.detail))


def _notify(rt: Runtime, action) -> None:
    """Alles, was die Regelung stoppt, ist kritisch (Push plus HA-Benachrichtigung). Lesefehler nennen Hebel oder
    Rollen (room_target, room_actual); beide werden mit ihrer Entity genannt."""
    binding = rt.override.binding
    refs = {
        **rt.manifest.entity_ids,
        **{lever: binding.ref(lever) for lever in binding.description.lever_set.levers if binding.has(lever)},
    }
    text = delivery.notification_text(action.kind, action.detail, refs)
    key, state = _notice(action.kind, action.detail)
    rt.notifier.notify(key, state, text, critical=True)


def _record_answer(rt: Runtime) -> None:
    """Jede Antwort auf den offenen Tick zeigt, dass der Server lebt (Status letzte_serverantwort)."""
    try:
        rt.store.update(last_ack_at=wallclock.now().isoformat(timespec="seconds"))
    except Exception:
        logger.exception("Zeitpunkt der Serverantwort konnte nicht gespeichert werden")


def _clear_sent_manual_override(rt: Runtime) -> None:
    """Der Server hat den Snapshot mit dem (schon zurueckgesetzten) Eingriff verarbeitet (KPI
    erfasst). Nur genau der gesendete Eintrag wird geloescht; ein inzwischen neu erkannter reist
    mit dem naechsten Tick."""
    sent = rt.manual_override_sent
    if sent is None or rt.store.state.manual_override_pending != sent:
        return
    rt.manual_override_sent = None
    try:
        rt.store.update(manual_override_pending=None)
    except Exception:
        logger.exception("Uebertragener manueller Eingriff konnte nicht als erledigt gespeichert werden")


def _valid_levers(levers, expected: tuple[str, ...]) -> TypeGuard[dict[str, float]]:
    """Genau die Hebel des Hebelsatzes, jeder eine endliche Zahl (kein JSON true): eine Antwort mit fehlendem oder
    fremdem Hebel ist ungueltig, geschrieben wird dann nichts."""
    return (
        isinstance(levers, dict) and set(levers) == set(expected)
        and all(_is_finite_number(value) for value in levers.values())
    )


def handle_setpoints(rt: Runtime, payload: dict) -> None:
    """Server-Antwort (nur Schema 4), zaehlt nur fuer den offenen Tick (auch verspaetet). Gueltige
    Werte gehen vor dem Ack auf die Anlage. Ein unbekanntes Schema, ein unbekannter Status oder
    ungueltige Werte zaehlen als Datenfehler vom Server. Kann die Anlage die Werte nicht
    uebernehmen, ist das eine Antwort mit eigenem Datenfehler (`WriteFailed`), kein Serverausfall."""
    seq = payload.get("seq")
    state = rt.store.state.delivery
    if not delivery.accepts_ack(state, seq):
        pending_seq = state.pending.seq if state.pending is not None else None
        logger.info("Setpoints-Antwort mit seq=%r ignoriert (erwartet: %r)", seq, pending_seq)
        return
    # accepts_ack() liefert nur True, wenn seq == state.pending.seq (str) ist; andere
    # JSON-Typen (int/float/bool/list/dict) sind nie gleich einem str, seq ist also ein str.
    assert isinstance(seq, str)
    _record_answer(rt)

    schema = payload.get("schema")
    if type(schema) is not int or schema != SNAPSHOT_SCHEMA_VERSION:
        # Nur Schema 4 wird verstanden; alles andere zaehlt wie eine ungueltige Antwort (Datenfehler
        # vom Server, sofort sichtbar) statt still verworfen zu werden und erst ueber den
        # Ack-Timeout im Notbetrieb zu enden.
        reason = f"ungültige Serverantwort (unbekanntes Schema {schema!r})"
        logger.warning("Setpoints-Antwort (seq=%s): %s - nichts geschrieben", seq, reason)
        deliver(rt, delivery.Ack(seq=seq, status=delivery.STATUS_REJECTED, reason=reason))
        return

    status = payload.get("status")
    levers = payload.get("levers")
    expected = rt.override.binding.description.lever_set.levers
    if status in _SETPOINT_STATUSES_WITH_VALUES and _valid_levers(levers, expected):
        _clear_sent_manual_override(rt)
        if status == delivery.STATUS_SKIPPED:
            logger.info(
                "Server hat fuer seq=%s nicht gelernt (%s), Werte unveraendert uebernommen", seq, payload.get("reason"),
            )
        try:
            rt.override.apply_server_values({lever: levers[lever] for lever in expected})
        except DeviceWriteError as error:
            logger.warning("Serverwerte (seq=%s) konnten nicht auf die Anlage geschrieben werden: %s", seq, error)
            deliver(rt, delivery.WriteFailed(seq=seq, detail=str(error)))
            return
        except StorageError as error:
            logger.warning(
                "Serverwerte (seq=%s) nicht uebernommen, Wiederherstellungspunkt nicht speicherbar: %s", seq, error,
            )
            deliver(rt, delivery.AnsweredLocalFault(seq=seq, roles=(delivery.ROLE_DATENTRAEGER,)))
            return
        _store_learned(rt, payload.get("learned"))
        deliver(rt, delivery.Ack(seq=seq, status=status))
    elif status == delivery.STATUS_REJECTED:
        reason = payload.get("reason")
        reason = reason if isinstance(reason, str) and reason else None
        logger.warning("Server hat Snapshot (seq=%s) abgelehnt: %s", seq, reason)
        deliver(rt, delivery.Ack(seq=seq, status=delivery.STATUS_REJECTED, reason=reason))
    else:
        reason = f"ungültige Serverantwort (status={status!r}, levers={levers!r}, erwartet: {', '.join(expected)})"
        logger.warning("Setpoints-Antwort (seq=%s): %s - nichts geschrieben", seq, reason)
        deliver(rt, delivery.Ack(seq=seq, status=delivery.STATUS_REJECTED, reason=reason))
