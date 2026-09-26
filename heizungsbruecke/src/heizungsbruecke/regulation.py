"""Lokaler Check: Comfort- und Notfall-Boost entscheiden, Sollwert-Historie fuehren und
entscheiden, ob ein neuer Tick faellig ist. Welche Werte dann auf der Anlage stehen,
entscheidet override.set_boosts."""
import logging
import time
from datetime import datetime

from heizungsbruecke.boost import decide_boost
from heizungsbruecke.emergency_boost import decide_emergency_boost
from heizungsbruecke.runtime import Runtime
from heizungsbruecke.target_history import record_change

logger = logging.getLogger(__name__)


def read_room_target_live(rt: Runtime) -> float | None:
    """Liest room_target an HA vorbei am Stable-Target-Cache. Nur fuer Boot, den entprellten
    room_target-Trigger, (Wieder-)Verbinden und den Watchdog. None, wenn die Rolle fehlt."""
    if "room_target" not in rt.manifest.entity_ids:
        return None
    return rt.ha_api.get_state(rt.manifest.entity_ids["room_target"])


def refresh_stable_target(rt: Runtime) -> None:
    """Ist room_target nicht lesbar, bleibt der alte Wert: die Tick-Pruefung laeuft weiter, und
    der Versuch meldet den Fuehler dann als Datenfehler."""
    try:
        value = read_room_target_live(rt)
    except Exception as error:
        logger.warning(
            "room_target nicht lesbar, Stable-Target-Cache bleibt bei %s: %s", rt.store.state.stable_target, error,
        )
        return
    rt.store.update(stable_target=value)
    logger.info("Stable-Target-Cache aktualisiert: room_target=%s", value)


def run_local_check(rt: Runtime) -> None:
    """Comfort-Boost nur bei Sollwerterhoehung, Notfall-Boost nur waehrend Notbetrieb. room_target
    kommt aus dem Stable-Target-Cache, damit ein Zwischenwert beim Verstellen keinen Boost
    ausloest. I/O-Fehler gehen an den Aufrufer."""
    manifest, options = rt.manifest, rt.options
    if "room_actual" not in manifest.entity_ids or "room_target" not in manifest.entity_ids:
        return
    state = rt.store.state
    room_target = state.stable_target
    if room_target is None:
        logger.warning("Lokaler Check uebersprungen: Stable-Target-Cache noch nicht befuellt (room_target=None)")
        return
    if state.abo_finished:
        return

    room_actual = rt.ha_api.get_state(manifest.entity_ids["room_actual"])
    threshold_k = options.get("boost_threshold_k", 0.5)
    comfort = decide_boost(
        room_actual=room_actual,
        room_target=room_target,
        previous_room_target=state.last_room_target,
        boost_was_active=state.boost_active,
        arrival_threshold_k=threshold_k,
        boost_curve_value=options["boost_curve_value"],
        boost_offset_value=options["boost_offset_value"],
    ).active
    emergency = state.delivery.notbetrieb and decide_emergency_boost(
        room_actual=room_actual,
        room_target=room_target,
        emergency_was_active=state.emergency_boost_active,
        exit_threshold_k=threshold_k,
        max_curve_value=options["curve_max"],
        max_offset_value=options["offset_max"],
    ).active
    comfort_set, _ = rt.override.set_boosts(comfort=comfort, emergency=emergency)

    # B5: wurde der Comfort-Start mangels Wiederherstellungspunkt abgelehnt, bleibt der alte
    # Sollwert gemerkt, damit der naechste Check die Erhoehung erneut sieht.
    remembered = state.last_room_target if comfort and not comfort_set else room_target
    rt.store.update(
        last_room_target=remembered,
        target_history=record_change(state.target_history, time.time(), room_target),
    )


def claim_due_tick(rt: Runtime, now: datetime) -> str | None:
    """Neuer Tick, wenn sich room_target seit dem letzten Tick geaendert hat ("target_change")
    oder daily_trigger_time heute erstmals erreicht ist ("daily"). Gebucht wird beim Entstehen,
    nicht beim Publish: ein zurueckgehaltener Tick erzeugt keine Duplikate, seine
    Wiederholungen laufen ueber die Zustellung."""
    state = rt.store.state
    room_target = state.stable_target
    if state.abo_inactive_since is not None or room_target is None:
        return None
    today = now.date().isoformat()
    daily_trigger_time = rt.options.get("daily_trigger_time")
    daily_due = False
    if daily_trigger_time:
        trigger_time = datetime.strptime(daily_trigger_time, "%H:%M").time()
        daily_due = now.time() >= trigger_time and state.last_daily_trigger_date != today
    target_changed = room_target != state.last_published_target_rt
    if not (daily_due or target_changed):
        return None
    changes = {"last_published_target_rt": room_target}
    if daily_due:
        changes["last_daily_trigger_date"] = today
    previous = {key: getattr(state, key) for key in changes}
    try:
        rt.store.update(**changes)
    except Exception:
        # Wie 0.16.0: ohne gespeicherte Buchung kein Tick; die Buchung wird im Speicher
        # zurueckgenommen, damit der naechste Check den Tick erneut beansprucht.
        try:
            rt.store.update(**previous)
        except Exception:
            pass  # scheitert erwartungsgemaess ebenso, der Speicher ist trotzdem zurueckgesetzt
        raise
    return "target_change" if target_changed else "daily"
