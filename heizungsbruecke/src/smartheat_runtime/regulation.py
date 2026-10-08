"""Lokaler Check: Comfort- und Notfall-Boost entscheiden, Sollwert-Historie fuehren und
entscheiden, ob ein neuer Tick faellig ist. Welche Werte dann auf der Anlage stehen,
entscheidet die Hebel-Pipeline (LeverPipeline.set_boosts)."""
import logging
from datetime import datetime

from smartheat_core import wallclock
from smartheat_core.boost import UNREADABLE_ROOM_CHECKS, comfort_boost_expired, decide_boost
from smartheat_core.emergency_boost import decide_emergency_boost
from smartheat_runtime.runtime import Runtime
from smartheat_runtime.waerme import parse_since

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

    try:
        room_actual = rt.signals.get_state(manifest.refs["room_actual"])
    except Exception:
        _count_unreadable_room(rt)
        raise
    if state.room_actual_misses:
        rt.store.update(room_actual_misses=0)
    now = wallclock.now()
    # Nicht lesbarer oder zeitzonenloser Zeitstempel = unbekannt (wie ein Boost vor dem Update), nie ein Abbruch.
    since = parse_since(state.boost_since)
    expired = state.boost_active and comfort_boost_expired(since, now)
    if expired:
        logger.info("Comfort-Boost endet nach der Hoechstdauer (seit %s)", state.boost_since)
    safety = rt.override.safety
    # Ohne Comfort-Boost-Werte (z. B. Fussbodenheizung, Spec 6.3) gibt es keinen Comfort-Boost.
    comfort = not expired and bool(safety.comfort_boost) and decide_boost(
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
    was_active = state.boost_active
    comfort_set, _ = rt.override.set_boosts(comfort=comfort, emergency=emergency)
    _track_boost_since(rt, now, was_active)

    # B5: wurde der Comfort-Start mangels Wiederherstellungspunkt abgelehnt, bleibt der alte
    # Sollwert gemerkt, damit der naechste Check die Erhoehung erneut sieht.
    remembered = state.last_room_target if comfort and not comfort_set else room_target
    rt.store.update_saved(last_room_target=remembered)


def _track_boost_since(rt: Runtime, now: datetime, was_active: bool) -> None:
    """Beginn des Comfort-Boosts merken bzw. loeschen. Startet der Boost jetzt (vorher inaktiv), gilt `now` und ein
    uebrig gebliebener alter Zeitstempel wird ueberschrieben; ein laufender Boost behaelt seinen Zeitstempel, ein
    vor dem Update persistierter (ohne oder mit unlesbarem Zeitstempel) laeuft ab jetzt. update() statt
    update_saved(): bei einem Schreibfehler bleibt der Wert im Speicher, die 4-h-Grenze greift trotzdem."""
    state = rt.store.state
    if not state.boost_active:
        since = None
    elif not was_active or parse_since(state.boost_since) is None:
        since = now.isoformat()
    else:
        since = state.boost_since
    if since != state.boost_since:
        rt.store.update(boost_since=since)


def _count_unreadable_room(rt: Runtime) -> None:
    """Audit 4, A4-09: nach UNREADABLE_ROOM_CHECKS unlesbaren Raumwerten in Folge endet ein laufender Comfort-Boost
    (Rueckkehr auf den Wiederherstellungspunkt); der Notfall-Boost bleibt, wie er ist."""
    misses = rt.store.state.room_actual_misses + 1
    rt.store.update(room_actual_misses=misses)
    if misses >= UNREADABLE_ROOM_CHECKS and rt.store.state.boost_active:
        logger.warning("Raumfuehler %d-mal in Folge nicht lesbar, Comfort-Boost endet", misses)
        rt.override.set_boosts(comfort=False, emergency=rt.store.state.emergency_boost_active)
        _track_boost_since(rt, wallclock.now(), was_active=True)


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
