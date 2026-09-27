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
from heizungsbruecke.derived_sensors import DerivedSensors
from heizungsbruecke.ha_api import HomeAssistantApi
from heizungsbruecke.manifest import ManifestError, build_manifest
from heizungsbruecke.notifier import STATE_OK, Notifier
from heizungsbruecke.override import Override
from heizungsbruecke.runtime import (
    EV_ACK_TIMEOUT, EV_AUTH_REJECTED, EV_DAYNIGHT, EV_GRACE_CHECK, EV_HA_CONNECTED, EV_LOCAL_CHECK,
    EV_MQTT_CONNECTED, EV_RETRY_DUE, EV_SETPOINTS, EV_TELEMETRY, EV_WATCHDOG, Runtime,
)
from heizungsbruecke.state import StateStore
from heizungsbruecke.status import STATUS_BEREIT, STATUS_KONFIGURATIONSFEHLER, STATUS_STARTET, StatusReporter
from heizungsbruecke.worker import Event, RegulationWorker

# Das Add-on startet mit `startup: services`, evtl. vor HA Core. Solange HA nicht antwortet,
# wird unbegrenzt gewartet (B10). Erst bei erreichbarem HA zaehlt das Budget fuer fehlende
# Entities und nicht anlegbare Hilfs-Entities (~4 min, Cloud-Integrationen laden spaet);
# danach ist es ein Startfehler (Spec TP6 3.6).
HA_REACHABILITY_DELAYS_SECONDS = (5, 10, 20, 40, 60)
DERIVED_SENSORS_RETRY_DELAYS_SECONDS = (5, 10, 20, 40, 60, 60, 60)
REQUIRED_ENTITY_OPTIONS = (
    "entity_room_target", "entity_curve_current", "entity_offset_current", "entity_heat_limit", "entity_outdoor_temp",
)
CONFIG_OK_MESSAGE = "SmartHeat: Einrichtung in Ordnung, die Heizungssteuerung läuft."
CONFIG_ERROR_MESSAGE = (
    "SmartHeat: Konfigurationsfehler – {grund}. Die Heizungssteuerung ist gestoppt, "
    "die Anlage behält ihre letzten Werte."
)
REDACTED = "***"
SOURCE_CHANGE_MESSAGE = (
    "SmartHeat: Die Quelle der Raum- oder Außentemperatur hat sich geändert. Die Tagesmittel "
    "(DAT/DART) sind erst nach 24 Stunden wieder vollständig."
)

logger = logging.getLogger(__name__)


class StartupError(Exception):
    """Startfehler bei erreichbarem HA nach Ablauf des Budgets (Spec TP6 3.6)."""


class _MissingEntities(Exception):
    pass


def _wait_until_reachable(ha_api) -> None:
    attempt = 0
    while not ha_api.is_reachable():
        if attempt == 0:
            logger.info("Home Assistant ist noch nicht erreichbar, warte ...")
        time.sleep(HA_REACHABILITY_DELAYS_SECONDS[min(attempt, len(HA_REACHABILITY_DELAYS_SECONDS) - 1)])
        attempt += 1


def _retry_with_budget(ha_api, attempt, describe):
    """Ruft attempt() bis zum Erfolg. Ein Fehler bei nicht erreichbarem HA wartet unbegrenzt,
    ohne das Budget zu verbrauchen; bei erreichbarem HA gilt DERIVED_SENSORS_RETRY_DELAYS_SECONDS,
    danach StartupError(describe(letzter Fehler))."""
    used = 0
    while True:
        try:
            return attempt()
        except Exception as error:
            if not ha_api.is_reachable():
                _wait_until_reachable(ha_api)
                continue
            if used >= len(DERIVED_SENSORS_RETRY_DELAYS_SECONDS):
                raise StartupError(describe(error)) from error
            logger.warning("Start noch nicht moeglich (Versuch %s/%s): %s",
                           used + 1, len(DERIVED_SENSORS_RETRY_DELAYS_SECONDS) + 1, error)
            time.sleep(DERIVED_SENSORS_RETRY_DELAYS_SECONDS[used])
            used += 1


def _wait_for_required_entities(ha_api, options: dict) -> None:
    """Pflicht-Entities muessen existieren; `unavailable` ist kein Startfehler (das behandeln
    Datenfehler und Notbetrieb im Betrieb)."""
    refs = [options[key] for key in REQUIRED_ENTITY_OPTIONS] + list(options["room_sensors"])
    entity_ids = sorted({ref.partition("::")[0] for ref in refs})

    def _check():
        missing = [entity_id for entity_id in entity_ids if not ha_api.entity_exists(entity_id)]
        if missing:
            raise _MissingEntities(", ".join(missing))

    def _describe(error):
        if isinstance(error, _MissingEntities):
            return f"Entity fehlt in Home Assistant: {error}"
        return f"Entities konnten nicht geprüft werden: {error}"

    _retry_with_budget(ha_api, _check, _describe)


def _ensure_derived_sensors_with_retry(ha_api, options: dict) -> DerivedSensors:
    return _retry_with_budget(
        ha_api,
        lambda: derived_sensors.ensure_all(
            ha_api=ha_api,
            tenant_id=options["tenant_id"],
            room_sensors=options["room_sensors"],
            outdoor_source=options["entity_outdoor_temp"],
            avg_window_hours=options["avg_window_hours"],
            state_path=config.DERIVED_SENSORS_PATH,
        ),
        lambda error: f"Hilfs-Entities konnten nicht angelegt werden: {error}",
    )


def _without_credentials(text: str, options: dict) -> str:
    """Startfehler zitieren ungueltige Optionswerte (!r) und landen in Status-Entity, Meldungen
    und Log. Steht das MQTT-Passwort (versehentlich) in so einem Wert, wird es unkenntlich
    gemacht, auch in der von repr() maskierten Form (Regel 6)."""
    password = options.get("mqtt_password")
    if isinstance(password, str) and password:
        for variant in (password, repr(password)[1:-1]):
            text = text.replace(variant, REDACTED)
    return text


def _fail_start(notifier, status, grund: str) -> int:
    logger.error("FEHLER: %s", grund)
    notifier.notify("konfiguration", f"fehler:{grund}", CONFIG_ERROR_MESSAGE.format(grund=grund), critical=True)
    status.set(STATUS_KONFIGURATIONSFEHLER, grund=grund)
    return 1


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
    if rt.status is not None and rt.status.state != STATUS_BEREIT:
        rt.status.set(STATUS_BEREIT)
    ticks.deliver(rt, delivery.MqttConnected())


def _on_ha_connected(rt: Runtime, event: Event) -> None:
    if rt.status is not None:
        rt.status.republish()


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
        EV_HA_CONNECTED: _on_ha_connected,
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
    1 = Konfigurationsfehler (gemeldet ueber notifier und Status-Entity)."""
    if not config.is_configured(options):
        logger.info(
            "Add-on ist noch nicht eingerichtet -- bitte die SmartHeat-Integration in "
            "Home Assistant installieren und dort die Verbindung zu diesem Add-on "
            "einrichten (sie schreibt die Konfiguration automatisch per Supervisor-API). "
            "Die Regelung startet erst, sobald options.json vollstaendig ist, und "
            "danach automatisch beim naechsten Neustart des Add-ons."
        )
        return 0
    # Erst warten, bis HA antwortet: Status und Meldungen sollen ankommen, und ohne HA geht
    # ohnehin nichts (Hilfs-Entities, Anlage).
    _wait_until_reachable(ha_api)
    store = StateStore(config.BACKUP_PATH, config.FAILSAFE_PATH)
    notifier = Notifier(store, ha_api, config.notify_services(options))
    ticks.seed_notices(notifier, store.state.delivery)
    status = StatusReporter(ha_api, options["tenant_id"], options.get("setup_id"))
    status.set(STATUS_STARTET)
    try:
        options = config.resolve_effective_options(options)
        error = config.validate(options)
        if error:
            raise config.ConfigError(error)
        _wait_for_required_entities(ha_api, options)
        derived = _ensure_derived_sensors_with_retry(ha_api, options)
        manifest = build_manifest(options, derived.entity_ids)
    except (config.ConfigError, ManifestError, StartupError) as error:
        return _fail_start(notifier, status, _without_credentials(str(error), options))

    notifier.notify("konfiguration", STATE_OK, CONFIG_OK_MESSAGE, critical=True)
    if derived.replaced:
        notifier.notify("quellwechsel", derived.sources_fingerprint, SOURCE_CHANGE_MESSAGE, critical=False)
    _check_timezone(ha_api)
    rt = Runtime(
        manifest=manifest, ha_api=ha_api, options=options, derived_entity_ids=derived.entity_ids,
        worker=RegulationWorker(clock=clock), store=store, override=Override(store, manifest, ha_api, options),
        notifier=notifier, status=status,
    )

    # Abo-Status erst hier: Abschluss-Start und lokaler Modus brauchen Manifest und Clamps.
    # "unknown" (accounts-api nicht erreichbar) startet normal -- fail-open.
    now = datetime.now().astimezone()
    abo_status = entitlement.query_status(options["tenant_id"], options["accounts_api_base_url"])
    if abo_status == entitlement.ACTIVE:
        entitlement.clear(config.ENTITLEMENT_PATH)
        notifier.notify("abo", STATE_OK, abo.ABO_ACTIVE_MESSAGE, critical=True)
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
    if abo_inactive:
        # Ohne MQTT kein EV_MQTT_CONNECTED: die lokale Regelung laeuft, also bereit.
        status.set(STATUS_BEREIT)
    return rt


def _run_bridge(options: dict, ha_api) -> int:
    """Exit-Code: 0 = nicht konfiguriert oder Abo-Frist regulaer abgeschlossen (ohne
    `watchdog` in config.yaml bleibt das Add-on dann gestoppt), 1 = Konfigurationsfehler
    (vorher gemeldet).
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
