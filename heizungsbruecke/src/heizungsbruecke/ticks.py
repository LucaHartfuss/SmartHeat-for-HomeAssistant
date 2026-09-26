"""Tick-Zustellung ausfuehren: die Aktionen der Zustandsmaschine `delivery` (Versuch,
Abo-Abfrage, Zeitplan, Meldungen) und die Server-Antworten."""
import logging
import math
import time
import uuid
from datetime import datetime

from heizungsbruecke import abo, config, delivery, entitlement
from heizungsbruecke.override import DeviceWriteError
from heizungsbruecke.runtime import EV_ACK_TIMEOUT, EV_RETRY_DUE, Runtime
from heizungsbruecke.snapshot import publish_snapshot, read_snapshot_roles
from heizungsbruecke.target_history import time_weighted_mean
from heizungsbruecke.worker import Event

logger = logging.getLogger(__name__)

_SETPOINT_STATUSES_WITH_VALUES = (delivery.STATUS_OK, delivery.STATUS_SKIPPED_SUMMER)


def _is_finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def start_tick(rt: Runtime, trigger: str) -> None:
    deliver(rt, delivery.TickDue(seq=str(uuid.uuid4()), trigger=trigger))


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
        status = entitlement.query_status(rt.options["tenant_id"], config.ACCOUNTS_API_BASE_URL)
        return delivery.EntitlementChecked(seq=action.seq, status=status)
    if isinstance(action, delivery.ScheduleAckTimeout):
        rt.worker.schedule(action.delay_s, Event(EV_ACK_TIMEOUT, {"seq": action.seq, "gen": action.gen}))
    elif isinstance(action, delivery.ScheduleRetry):
        rt.worker.schedule(action.delay_s, Event(EV_RETRY_DUE, {"seq": action.seq, "gen": action.gen}))
    elif isinstance(action, delivery.EnterAboInactive):
        abo.enter_inactive(rt, datetime.now().astimezone())
    elif isinstance(action, delivery.Notify):
        _notify(rt, action)
    elif isinstance(action, delivery.PublishFailsafe):
        if rt.mqtt_client is not None:
            rt.mqtt_client.publish_status("failsafe", delivery.build_state_payload(action.active))
    elif isinstance(action, delivery.EndEmergencyBoost):
        rt.override.set_boosts(comfort=rt.store.state.boost_active, emergency=False)
    else:
        raise TypeError(f"Unbekannte Zustell-Aktion: {action!r}")
    return None


def _attempt(rt: Runtime, seq: str, trigger: str):
    """Pflichtrollen und room_actual frisch lesen; bei ungueltigem Wert kein Publish
    (ReadInvalid). Auch ein Publish-Fehler meldet Published: der Ack-Timeout plant dann den
    naechsten Versuch, die Retry-Kette reisst nie ab."""
    read = read_snapshot_roles(rt.manifest, rt.ha_api, computed_values={"room_target_avg_24h": _current_target_avg(rt)})
    if read.invalid_roles:
        logger.warning("Snapshot (seq=%s) zurueckgehalten, ungueltige Werte: %s", seq, ", ".join(read.invalid_roles))
        return delivery.ReadInvalid(seq=seq, roles=read.invalid_roles)
    if rt.mqtt_client is None or not rt.mqtt_client.is_connected():
        # Ohne Verbindung nicht publizieren: paho wuerde QoS-1-Nachrichten stauen und nach einem
        # langen Ausfall einen Stunden alten Messwertsatz nachliefern. Der Ack-Timeout plant den
        # naechsten Versuch, das (Wieder-)Verbinden startet ihn sofort.
        logger.warning("Snapshot (seq=%s) nicht gesendet, keine MQTT-Verbindung", seq)
        return delivery.Published(seq=seq)
    try:
        publish_snapshot(rt.mqtt_client, seq=seq, trigger=trigger, roles=read.roles)
        logger.info("Voller Snapshot veroeffentlicht (seq=%s, trigger=%s)", seq, trigger)
    except Exception:
        logger.exception("Snapshot (seq=%s) konnte nicht veroeffentlicht werden - Retry nach dem Ack-Timeout", seq)
    return delivery.Published(seq=seq)


def _current_target_avg(rt: Runtime) -> float | None:
    """Zeitgewichtetes 24-h-Mittel des Sollwerts (room_target_avg_24h)."""
    try:
        return time_weighted_mean(rt.store.state.target_history, time.time())
    except Exception as error:
        logger.warning("Sollwert-Mittel nicht berechenbar, wird weggelassen: %s", error)
        return None


def _notify(rt: Runtime, action) -> None:
    text = delivery.notification_text(action.kind, action.detail, rt.manifest.entity_ids)
    logger.warning(text)
    notify_service = rt.options.get("notify_service", "")
    if notify_service:
        try:
            rt.ha_api.send_notification(notify_service, text)
        except Exception:
            logger.warning("Push-Benachrichtigung (%s) konnte nicht gesendet werden", action.kind)


def handle_setpoints(rt: Runtime, payload: dict) -> None:
    """Server-Antwort (Schema 2), zaehlt nur fuer den offenen Tick (auch verspaetet). Gueltige
    Werte gehen vor dem Ack auf die Anlage. Ein unbekannter Status oder ungueltige Werte zaehlen
    als Datenfehler vom Server. Kann die Anlage die Werte nicht uebernehmen, ist das eine
    Antwort mit eigenem Datenfehler (`WriteFailed`), kein Serverausfall."""
    seq = payload.get("seq")
    state = rt.store.state.delivery
    if not delivery.accepts_ack(state, seq):
        expected = state.pending.seq if state.pending is not None else None
        logger.info("Setpoints-Antwort mit seq=%r ignoriert (erwartet: %r)", seq, expected)
        return

    status = payload.get("status")
    curve, offset = payload.get("curve"), payload.get("offset")
    if status in _SETPOINT_STATUSES_WITH_VALUES and _is_finite_number(curve) and _is_finite_number(offset):
        try:
            rt.override.apply_server_values(curve, offset)
        except DeviceWriteError as error:
            logger.warning("Serverwerte (seq=%s) konnten nicht auf die Anlage geschrieben werden: %s", seq, error)
            deliver(rt, delivery.WriteFailed(seq=seq, detail=str(error)))
            return
        deliver(rt, delivery.Ack(seq=seq, status=status))
    elif status == delivery.STATUS_REJECTED:
        reason = payload.get("reason")
        reason = reason if isinstance(reason, str) and reason else None
        logger.warning("Server hat Snapshot (seq=%s) abgelehnt: %s", seq, reason)
        deliver(rt, delivery.Ack(seq=seq, status=delivery.STATUS_REJECTED, reason=reason))
    else:
        reason = f"ungültige Serverantwort (status={status!r}, curve={curve!r}, offset={offset!r})"
        logger.warning("Setpoints-Antwort (seq=%s): %s - nichts geschrieben", seq, reason)
        deliver(rt, delivery.Ack(seq=seq, status=delivery.STATUS_REJECTED, reason=reason))
