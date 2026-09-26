"""Einstieg des Add-ons: Boot, Handler des Regel-Workers, Hauptschleife.

Alle Regelungsereignisse laufen nacheinander im Hauptthread (RegulationWorker). Die Handler
hier sind duenn und rufen das zustaendige Modul."""
import functools
import logging
import os
import sys
import time
from datetime import datetime

from heizungsbruecke import (
    abo, config, daynight_snapshot, delivery, derived_sensors, entitlement, regulation, telemetry, ticks, triggers,
)
from heizungsbruecke.ha_api import HomeAssistantApi
from heizungsbruecke.manifest import ManifestError, build_manifest
from heizungsbruecke.override import Override
from heizungsbruecke.runtime import (
    EV_ACK_TIMEOUT, EV_AUTH_REJECTED, EV_DAYNIGHT, EV_GRACE_CHECK, EV_LOCAL_CHECK, EV_MQTT_CONNECTED,
    EV_RETRY_DUE, EV_SETPOINTS, EV_TELEMETRY, EV_WATCHDOG, Runtime,
)
from heizungsbruecke.state import StateStore
from heizungsbruecke.worker import Event, RegulationWorker

# Das Add-on startet mit `startup: services`, evtl. vor HA Core: erst das Budget mit Backoff,
# danach unbegrenzt weiter. Ein spaet startendes HA ist kein Konfigurationsfehler.
DERIVED_SENSORS_RETRY_DELAYS_SECONDS = (5, 10, 20, 40, 60, 60, 60)
DERIVED_SENSORS_UNBOUNDED_RETRY_SECONDS = 300
HELPER_NOTIFICATION_TITLE = "SmartHeat"
HELPER_NOTIFICATION_ID = "smartheat_hilfssensoren"
HELPER_NOTIFICATION_MESSAGE = (
    "SmartHeat: Hilfssensoren konnten nicht angelegt werden – Home Assistant noch nicht bereit?"
)

logger = logging.getLogger(__name__)


def _ensure_derived_sensors_with_retry(ha_api, options: dict) -> dict[str, str]:
    """derived_sensors.ensure_all mit Retry: erst das Budget, dann unbegrenzt alle
    DERIVED_SENSORS_UNBOUNDED_RETRY_SECONDS. Beim Uebergang einmal ERROR und best effort eine
    HA-Benachrichtigung. Wirft nie."""
    delays = DERIVED_SENSORS_RETRY_DELAYS_SECONDS
    attempt = 0
    while True:
        try:
            return derived_sensors.ensure_all(
                ha_api=ha_api,
                tenant_id=options["tenant_id"],
                room_actual_entity_id=options["entity_room_actual"],
                outdoor_temp_entity_id=options["entity_outdoor_temp"],
                avg_window_hours=options["avg_window_hours"],
                state_path=config.DERIVED_SENSORS_PATH,
            )
        except Exception as error:
            if attempt < len(delays):
                logger.warning(
                    "Anlegen der abgeleiteten Sensoren fehlgeschlagen (Versuch %s/%s, evtl. ist "
                    "HA Core beim Start des Add-ons noch nicht bereit): %s",
                    attempt + 1, len(delays) + 1, error,
                )
                delay = delays[attempt]
            else:
                if attempt == len(delays):
                    logger.error(
                        "Abgeleitete Sensoren nach %s Versuchen nicht angelegt, weiter alle %s s: %s",
                        attempt + 1, DERIVED_SENSORS_UNBOUNDED_RETRY_SECONDS, error,
                    )
                    try:
                        ha_api.create_persistent_notification(
                            HELPER_NOTIFICATION_TITLE, HELPER_NOTIFICATION_MESSAGE, HELPER_NOTIFICATION_ID,
                        )
                    except Exception:
                        logger.warning("HA-Benachrichtigung zu den Hilfssensoren konnte nicht angelegt werden")
                else:
                    logger.warning("Anlegen der abgeleiteten Sensoren weiter fehlgeschlagen (Versuch %s): %s", attempt + 1, error)
                delay = DERIVED_SENSORS_UNBOUNDED_RETRY_SECONDS
            attempt += 1
            time.sleep(delay)


def _check_timezone(ha_api) -> None:
    """Taegliche Zeitpunkte (Tagestick, Tag-/Nachtmittel) laufen in der Container-Zeitzone.
    Weicht sie von der HA-Zeitzone ab, nur warnen; kein Abbruch."""
    try:
        ha_time_zone = ha_api.get_config().get("time_zone")
    except Exception as error:
        logger.warning("Zeitzone von Home Assistant konnte nicht abgefragt werden: %s", error)
        return
    container_time_zone = os.environ.get("TZ") or str(datetime.now().astimezone().tzinfo)
    if ha_time_zone and ha_time_zone != container_time_zone:
        logger.warning(
            "Zeitzone weicht ab: Home Assistant '%s', Add-on-Container '%s' - taegliche "
            "Zeitpunkte laufen in der Container-Zeit.", ha_time_zone, container_time_zone,
        )
    else:
        logger.info("Zeitzone: %s", container_time_zone)


# --- Handler des Regel-Workers ---

def _on_local_check(rt: Runtime, event: Event) -> None:
    """Bei room_target_fired den Cache frisch lesen, dann Boost-Logik, dann "Tick faellig?".
    Beides getrennt abgesichert: ein toter room_actual-Fuehler laesst den Boost-Teil scheitern,
    darf aber keinen Tick verhindern; dessen Versuch meldet den Fuehler als Datenfehler."""
    if rt.store.state.abo_finished:
        return
    if event.data.get("room_target_fired"):
        regulation.refresh_stable_target(rt)
    try:
        regulation.run_local_check(rt)
    except Exception:
        logger.exception("Fehler im lokalen Check (Boost/Notfall-Boost), wird beim naechsten Ereignis erneut versucht")
    trigger = regulation.claim_due_tick(rt, datetime.now())
    if trigger is not None:
        ticks.start_tick(rt, trigger)


def _on_setpoints(rt: Runtime, event: Event) -> None:
    ticks.handle_setpoints(rt, event.data["payload"])


def _on_ack_timeout(rt: Runtime, event: Event) -> None:
    ticks.deliver(rt, delivery.AckTimeout(seq=event.data["seq"], gen=event.data["gen"]))


def _on_retry_due(rt: Runtime, event: Event) -> None:
    ticks.deliver(rt, delivery.RetryDue(seq=event.data["seq"], gen=event.data["gen"]))


def _on_mqtt_connected(rt: Runtime, event: Event) -> None:
    ticks.deliver(rt, delivery.MqttConnected())


def _on_auth_rejected(rt: Runtime, event: Event) -> None:
    abo.handle_auth_rejected(rt)


def _on_watchdog(rt: Runtime, event: Event) -> None:
    """Fallback bei getrennter WS-Verbindung: Cache frisch (ohne Entprellung) lesen, dann
    lokaler Check. Bei verbundenem Trigger-Client passiert nichts."""
    rt.worker.schedule(config.local_check_interval(rt.options), Event(EV_WATCHDOG))
    if rt.trigger_client is None or not rt.trigger_client.connected:
        rt.worker.post_coalesced(EV_LOCAL_CHECK, room_target_fired=True)


def _on_telemetry(rt: Runtime, event: Event) -> None:
    rt.worker.schedule(config.telemetry_interval(rt.options), Event(EV_TELEMETRY))
    state = rt.store.state
    if state.abo_inactive_since is not None or rt.mqtt_client is None:
        return
    telemetry.run_telemetry_tick(
        rt.manifest, rt.ha_api, rt.mqtt_client,
        boost_active=state.boost_active, failsafe_active=state.delivery.notbetrieb,
    )


def _on_daynight(rt: Runtime, event: Event) -> None:
    rt.worker.schedule(config.local_check_interval(rt.options), Event(EV_DAYNIGHT))
    daynight_snapshot.maybe_snapshot(
        ha_api=rt.ha_api,
        room_12h_avg_entity_id=rt.derived_entity_ids["_room_12h_avg"],
        day_avg_entity_id=rt.derived_entity_ids["room_day_avg"],
        night_avg_entity_id=rt.derived_entity_ids["room_night_avg"],
        day_avg_window_end=rt.options["day_avg_window_end"],
        night_avg_window_end=rt.options["night_avg_window_end"],
        state_path=config.DAYNIGHT_SNAPSHOT_PATH,
        now=datetime.now(),
    )


def _on_grace_check(rt: Runtime, event: Event) -> None:
    rt.worker.schedule(config.local_check_interval(rt.options), Event(EV_GRACE_CHECK))
    abo.check_grace_end(rt)


def _register_handlers(rt: Runtime) -> None:
    handlers = {
        EV_LOCAL_CHECK: _on_local_check,
        EV_SETPOINTS: _on_setpoints,
        EV_AUTH_REJECTED: _on_auth_rejected,
        EV_ACK_TIMEOUT: _on_ack_timeout,
        EV_RETRY_DUE: _on_retry_due,
        EV_MQTT_CONNECTED: _on_mqtt_connected,
        EV_WATCHDOG: _on_watchdog,
        EV_TELEMETRY: _on_telemetry,
        EV_DAYNIGHT: _on_daynight,
        EV_GRACE_CHECK: _on_grace_check,
    }
    for kind, handler in handlers.items():
        rt.worker.register(kind, functools.partial(handler, rt))


# --- Boot ---

def _prime(rt: Runtime) -> None:
    """Erster lokaler Check synchron vor mqtt.loop_start(): die Boost-Flags sind aus echten
    Sensorwerten bestimmt, bevor eine Server-Antwort verarbeitet wird (sie entscheiden, ob
    Serverwerte geschrieben oder nur gespeichert werden)."""
    try:
        # Wie 0.16.0: ein Comfort-Boost wird nach dem Neustart frisch bestimmt, ein laufender
        # endet ohne Zuruecksetzen der Anlage (Befund N1, TP5-Plan).
        rt.store.update(boost_active=False)
        rt.store.update(stable_target=regulation.read_room_target_live(rt))
        logger.info("Stable-Target-Cache initial befuellt (Boot-Priming): room_target=%s", rt.store.state.stable_target)
        regulation.run_local_check(rt)
    except Exception:
        logger.exception("Fehler beim initialen lokalen Check vor MQTT-Start, wird beim naechsten Ereignis erneut versucht")
        # Fail-open: veraltete Boost-Flags duerfen Serverwerte nicht dauerhaft vom Schreiben
        # abhalten und die Anlage so auf Boost-Werten festhalten.
        try:
            rt.store.update(boost_active=False, emergency_boost_active=False)
        except Exception:
            logger.exception("Boost-Flags konnten nach dem fehlgeschlagenen Start-Check nicht gespeichert werden")


def _start_bridge(options: dict, ha_api, clock=time.monotonic) -> Runtime | int:
    """Gibt den gestarteten Laufzeit-Kontext zurueck oder einen Exit-Code, wenn gar nicht erst
    geregelt wird: 0 = nicht konfiguriert oder Abo-Frist bereits abgeschlossen,
    1 = Konfigurationsfehler."""
    if not config.is_configured(options):
        logger.info(
            "Add-on ist noch nicht eingerichtet -- bitte die SmartHeat-Integration in "
            "Home Assistant installieren und dort die Verbindung zu diesem Add-on "
            "einrichten (sie schreibt die Konfiguration automatisch per Supervisor-API). "
            "Die Regelung startet erst, sobald options.json vollstaendig ist, und "
            "danach automatisch beim naechsten Neustart des Add-ons."
        )
        return 0
    try:
        options = config.resolve_effective_options(options)
    except config.ConfigError as error:
        logger.error("FEHLER: %s", error)
        return 1
    error = config.validate(options)
    if error:
        logger.error("FEHLER: %s", error)
        return 1

    derived_entity_ids = _ensure_derived_sensors_with_retry(ha_api, options)
    _check_timezone(ha_api)
    try:
        manifest = build_manifest(options, derived_entity_ids)
    except ManifestError as error:
        logger.error("FEHLER: %s", error)
        return 1

    store = StateStore(config.BACKUP_PATH, config.FAILSAFE_PATH)
    rt = Runtime(
        manifest=manifest, ha_api=ha_api, options=options, derived_entity_ids=derived_entity_ids,
        worker=RegulationWorker(clock=clock), store=store, override=Override(store, manifest, ha_api, options),
    )

    # Abo-Status erst hier: Abschluss-Start und lokaler Modus brauchen Manifest und Clamps.
    # "unknown" (accounts-api nicht erreichbar) startet normal -- fail-open.
    now = datetime.now().astimezone()
    abo_status = entitlement.query_status(options["tenant_id"], config.ACCOUNTS_API_BASE_URL)
    if abo_status == entitlement.ACTIVE:
        entitlement.clear(config.ENTITLEMENT_PATH)
    elif abo_status == entitlement.INACTIVE:
        inactive_since = entitlement.load_inactive_since(config.ENTITLEMENT_PATH)
        if inactive_since is not None and entitlement.grace_expired(inactive_since, now):
            # Nicht mit liegengebliebenen Boost-Werten beenden, wenn HA/Cloud beim Booten noch
            # nicht erreichbar ist.
            while not abo.finish_grace(rt, always_restore=False, final_notice=False):
                retry_seconds = config.local_check_interval(options)
                logger.error(
                    "Abo-inaktiv-Frist abgelaufen, Boost-Werte konnten nicht zurueckgesetzt werden - "
                    "erneuter Versuch in %s s", retry_seconds,
                )
                time.sleep(retry_seconds)
            return 0
        abo.enter_inactive(rt, now)

    _register_handlers(rt)
    abo_inactive = rt.store.state.abo_inactive_since is not None
    if not abo_inactive:
        rt.mqtt_client = triggers.create_mqtt_client(options, rt.worker, rt.store.state.delivery.notbetrieb)

    _prime(rt)
    if rt.mqtt_client is not None:
        rt.mqtt_client.loop_start()

    rt.trigger_client = triggers.build_ha_trigger_client(manifest, options, ha_api, rt.worker)
    rt.trigger_client.start()
    for kind in (EV_WATCHDOG, EV_TELEMETRY, EV_DAYNIGHT, EV_GRACE_CHECK):
        rt.worker.schedule(0, Event(kind))
    if not abo_inactive:
        # Offenen Tick aus failsafe_state.json sofort mit derselben seq erneut versuchen.
        ticks.deliver(rt, delivery.Boot())
    return rt


def _run_bridge(options: dict, ha_api) -> int:
    """Exit-Code: 0 = nicht konfiguriert oder Abo-Frist regulaer abgeschlossen (ohne
    `watchdog` in config.yaml bleibt das Add-on dann gestoppt), 1 = Konfigurationsfehler.
    Voruebergehende Fehler (HA nicht bereit, Broker nicht erreichbar, Server schweigt) beenden
    das Add-on nie."""
    started = _start_bridge(options, ha_api)
    if isinstance(started, int):
        return started
    return started.worker.run()


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    options = config.load_options_safe(config.OPTIONS_PATH)
    ha_api = HomeAssistantApi(base_url="http://supervisor", token=os.environ["SUPERVISOR_TOKEN"])

    exit_code = _run_bridge(options, ha_api)
    if exit_code:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
