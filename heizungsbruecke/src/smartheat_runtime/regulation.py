"""Lokaler Check: Comfort- und Notfall-Boost entscheiden, Sollwert-Historie fuehren und
entscheiden, ob ein neuer Tick faellig ist. Welche Werte dann auf der Anlage stehen,
entscheidet die Hebel-Pipeline (LeverPipeline.set_boosts)."""
import logging
from datetime import datetime

from smartheat_core.boost import decide_boost
from smartheat_core.emergency_boost import decide_emergency_boost
from smartheat_runtime.runtime import Runtime

logger = logging.getLogger(__name__)


def read_room_target_live(rt: Runtime) -> float | None:
    """Liest room_target an HA vorbei am Stable-Target-Cache. Nur fuer Boot, den entprellten
    room_target-Trigger, (Wieder-)Verbinden und den Watchdog. None, wenn die Rolle fehlt."""
    if "room_target" not in rt.manifest.refs:
        return None
    return rt.signals.get_state(rt.manifest.refs["room_target"])


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
    manifest = rt.manifest
    if "room_actual" not in manifest.refs or "room_target" not in manifest.refs:
        return
    state = rt.store.state
    room_target = state.stable_target
    if room_target is None:
        logger.warning("Lokaler Check uebersprungen: Stable-Target-Cache noch nicht befuellt (room_target=None)")
        return
    if state.abo_finished:
        return

    room_actual = rt.signals.get_state(manifest.refs["room_actual"])
    safety = rt.override.safety
    # Ohne Comfort-Boost-Werte (z. B. Fussbodenheizung, Spec 6.3) gibt es keinen Comfort-Boost.
    comfort = bool(safety.comfort_boost) and decide_boost(
        room_actual=room_actual,
        room_target=room_target,
        previous_room_target=state.last_room_target,
        boost_was_active=state.boost_active,
        arrival_threshold_k=safety.arrival_threshold_k,
    )
    emergency = state.delivery.notbetrieb and decide_emergency_boost(
        room_actual=room_actual,
        room_target=room_target,
        emergency_was_active=state.emergency_boost_active,
        exit_threshold_k=safety.arrival_threshold_k,
    )
    comfort_set, _ = rt.override.set_boosts(comfort=comfort, emergency=emergency)

    # B5: wurde der Comfort-Start mangels Wiederherstellungspunkt abgelehnt, bleibt der alte
    # Sollwert gemerkt, damit der naechste Check die Erhoehung erneut sieht.
    remembered = state.last_room_target if comfort and not comfort_set else room_target
    rt.store.update_saved(last_room_target=remembered)


def claim_due_tick(rt: Runtime, now: datetime) -> str | None:
    """Neuer Tick, wenn daily_trigger_time heute erstmals erreicht ist ("daily") oder sich
    room_target seit dem letzten Tick geaendert hat ("target_change"). Faellt beides auf denselben
    Check, geht der Tick als "daily" raus: der Server lernt nur auf "daily" und wendet die
    Vorsteuerung auf jeden Tick an, sonst fiele der Lernschritt des Tages aus. Gebucht wird beim
    Entstehen, nicht beim Publish: ein zurueckgehaltener Tick erzeugt keine Duplikate, seine
    Wiederholungen laufen ueber die Zustellung. Ohne Stable-Target-Cache (Thermostat beim Start
    nicht lesbar) gibt es nur einen faelligen Tagestick; dessen Versuch liest live und meldet den
    Fuehler als Datenfehler (AU-013)."""
    state = rt.store.state
    if state.abo_inactive_since is not None:
        return None
    room_target = state.stable_target
    today = now.date().isoformat()
    daily_trigger_time = rt.config.daily_trigger_time
    daily_due = False
    if daily_trigger_time:
        trigger_time = datetime.strptime(daily_trigger_time, "%H:%M").time()
        daily_due = now.time() >= trigger_time and state.last_daily_trigger_date != today
    if room_target is None:
        if not daily_due:
            return None
        rt.store.update_saved(last_daily_trigger_date=today)
        return "daily"
    target_changed = room_target != state.last_published_target_rt
    if not (daily_due or target_changed):
        return None
    changes: dict[str, object] = {"last_published_target_rt": room_target}
    if daily_due:
        changes["last_daily_trigger_date"] = today
    # Wie 0.16.0: ohne gespeicherte Buchung kein Tick; der naechste Check beansprucht ihn erneut.
    rt.store.update_saved(**changes)
    return "daily" if daily_due else "target_change"
