"""Einstieg des Add-ons: Boot, Handler des Regel-Workers, Hauptschleife.

Alle Regelungsereignisse laufen nacheinander im Hauptthread (RegulationWorker). Die Handler
hier sind duenn und rufen das zustaendige Modul. Das Add-on beendet sich nie absichtlich: mit
eingeschaltetem Supervisor-Watchdog wuerde auch ein sauberer Exit 0 neu gestartet (Spec TP7,
Befund Supervisor-Watchdog). Endzustaende sind ein Ruhezustand, in dem nur noch das
Lebenszeichen an die Integration laeuft."""
import functools
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime

from heizungsbruecke import (
    abo, battery, config, daynight_snapshot, delivery, derived_sensors, entitlement, manual_override, regulation,
    room_sensors, telemetry, ticks, triggers,
)
from heizungsbruecke.derived_sensors import DerivedSensors
from heizungsbruecke.ha_api import HomeAssistantApi
from heizungsbruecke.manifest import ChannelManifest, ManifestError, build_manifest
from heizungsbruecke.notifier import STATE_OK, Notifier
from heizungsbruecke.override import ROLES as OVERRIDE_ROLES
from heizungsbruecke.override import Override
from heizungsbruecke.runtime import (
    EV_ACK_TIMEOUT, EV_AUTH_REJECTED, EV_DAYNIGHT, EV_GRACE_CHECK, EV_HA_CONNECTED, EV_HEALTH, EV_HEARTBEAT,
    EV_LOCAL_CHECK, EV_MQTT_CONNECTED, EV_RECHECK, EV_RETRY_DUE, EV_SETPOINTS, EV_TELEMETRY, EV_WATCHDOG, Runtime,
)
from heizungsbruecke.state import StateStore
from heizungsbruecke.status import (
    ABO_AKTIV, HEARTBEAT_SECONDS, STATUS_ABGEMELDET, STATUS_ABO_BEENDET, STATUS_KONFIGURATIONSFEHLER, StatusReporter,
)
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
# Im Konfigurationsfehler prueft ein frischer Prozess nach dieser Zeit erneut (z. B. eine spaet
# geladene Integration); der persistierte Meldezustand verhindert eine Wiederholungsmeldung.
CONFIG_RECHECK_SECONDS = 900
IDLE_NOT_CONFIGURED = "nicht_eingerichtet"
CONFIG_OK_MESSAGE = "SmartHeat: Einrichtung in Ordnung, die Heizungssteuerung läuft."
CONFIG_ERROR_MESSAGE = (
    "SmartHeat: Konfigurationsfehler – {grund}. Die Heizungssteuerung ist gestoppt, "
    "die Anlage behält ihre letzten Werte."
)
SIGN_OFF_INVALID_CONFIG = "Konfiguration ungültig, die Anlage wurde nicht zurückgesetzt"
SIGN_OFF_RESTORE_FAILED = "Zurücksetzen der Anlage scheitert, neuer Versuch in {seconds} s"
REDACTED = "***"
SOURCE_CHANGE_MESSAGE = (
    "SmartHeat: Die Quelle der Raum- oder Außentemperatur hat sich geändert. Die Tagesmittel "
    "(DAT/DART) sind erst nach 24 Stunden wieder vollständig."
)

logger = logging.getLogger(__name__)


class StartupError(Exception):
    """Startfehler bei erreichbarem HA nach Ablauf des Budgets (Spec TP6 3.6). `key` ist die
    stabile Identitaet des Fehlers fuer den Meldezustand: der Fehlertext enthaelt oft
    laufzeitabhaengige Details (Flow-ID von HA, Objektadressen), die bei jedem Neustart anders
    waeren und sonst jedes Mal eine neue Meldung ausloesten (Review Focus 4)."""

    def __init__(self, grund: str, key: str) -> None:
        super().__init__(grund)
        self.key = key


class _MissingEntities(Exception):
    pass


@dataclass
class IdleBridge:
    """Ruhezustand beim Start (Spec TP7 3.2): keine Regelung, kein MQTT, keine Trigger, kein
    Schreiben auf die Anlage (ausser dem Zuruecksetzen beim Abmelden/Fristende). Es laeuft nur der
    Worker mit dem Lebenszeichen, sofern es einen Status-Kanal gibt."""
    worker: RegulationWorker
    status: StatusReporter | None
    reason: str  # IDLE_NOT_CONFIGURED oder ein Status-Wert


def _idle(clock, status: StatusReporter | None, reason: str, *, recheck_after: float | None = None) -> IdleBridge:
    worker = RegulationWorker(clock=clock)
    if status is not None:
        worker.register(EV_HEARTBEAT, functools.partial(_idle_heartbeat, worker, status))
        worker.after_each(status.publish_if_changed)
        worker.schedule(HEARTBEAT_SECONDS, Event(EV_HEARTBEAT))
        status.publish_if_changed()
    if recheck_after is not None:
        worker.register(EV_RECHECK, lambda event: abo.restart_process())
        worker.schedule(recheck_after, Event(EV_RECHECK))
    logger.info("Ruhezustand: %s", reason)
    return IdleBridge(worker=worker, status=status, reason=reason)


def _idle_heartbeat(worker: RegulationWorker, status: StatusReporter, event: Event) -> None:
    worker.schedule(HEARTBEAT_SECONDS, Event(EV_HEARTBEAT))
    status.publish()


def _wait_until_reachable(ha_api) -> None:
    attempt = 0
    while not ha_api.is_reachable():
        if attempt == 0:
            logger.info("Home Assistant ist noch nicht erreichbar, warte ...")
        time.sleep(HA_REACHABILITY_DELAYS_SECONDS[min(attempt, len(HA_REACHABILITY_DELAYS_SECONDS) - 1)])
        attempt += 1


def _retry_with_budget(ha_api, attempt, describe, *, identify=None, redact=lambda text: text):
    """Ruft attempt() bis zum Erfolg. Ein Fehler bei nicht erreichbarem HA wartet unbegrenzt,
    ohne das Budget zu verbrauchen; bei erreichbarem HA gilt DERIVED_SENSORS_RETRY_DELAYS_SECONDS,
    danach StartupError(describe(letzter Fehler), identify(letzter Fehler)); ohne identify ist
    der Text selbst die Identitaet. `redact` gilt fuer Log, Text und Identitaet."""
    used = 0
    while True:
        try:
            return attempt()
        except Exception as error:
            if not ha_api.is_reachable():
                _wait_until_reachable(ha_api)
                continue
            if used >= len(DERIVED_SENSORS_RETRY_DELAYS_SECONDS):
                grund = redact(describe(error))
                key = redact(identify(error)) if identify is not None else grund
                raise StartupError(grund, key) from error
            logger.warning("Start noch nicht moeglich (Versuch %s/%s): %s",
                           used + 1, len(DERIVED_SENSORS_RETRY_DELAYS_SECONDS) + 1, redact(str(error)))
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

    def _identify(error):
        if isinstance(error, _MissingEntities):
            return f"entity_fehlt:{error}"  # sortiert, also stabil
        return "entities_nicht_pruefbar"

    _retry_with_budget(
        ha_api, _check, _describe, identify=_identify, redact=functools.partial(_without_credentials, options=options),
    )


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
        identify=lambda error: "hilfs_entities",
        redact=functools.partial(_without_credentials, options=options),
    )


def _without_credentials(text: str, options: dict) -> str:
    """Startfehler zitieren ungueltige Optionswerte (!r) und landen im Status-Event, in Meldungen
    und im Log. Steht das MQTT-Passwort (versehentlich) in so einem Wert, wird es unkenntlich
    gemacht, auch in der von repr() maskierten Form (Regel 6)."""
    password = options.get("mqtt_password")
    if isinstance(password, str) and password:
        for variant in (password, repr(password)[1:-1]):
            text = text.replace(variant, REDACTED)
    return text


def _fail_start(notifier, status, clock, grund: str, key: str | None = None) -> IdleBridge:
    """Meldezustand `fehler:<key>` (stabile Identitaet, Standard: der Text selbst); Meldung und
    Status tragen den ausfuehrlichen Grund. Derselbe Fehler wie beim letzten Start pusht nicht
    erneut, legt aber die HA-Benachrichtigung neu an (sie fehlt nach einem Host-Neustart). Danach
    Ruhezustand mit Neupruefung nach CONFIG_RECHECK_SECONDS."""
    logger.error("FEHLER: %s", grund)
    message = CONFIG_ERROR_MESSAGE.format(grund=grund)
    if not notifier.notify("konfiguration", f"fehler:{grund if key is None else key}", message, critical=True):
        notifier.refresh_persistent("konfiguration", message)
    status.update(konfigurationsfehler=True, grund=grund)
    return _idle(clock, status, STATUS_KONFIGURATIONSFEHLER, recheck_after=CONFIG_RECHECK_SECONDS)


def _sign_off(options: dict, ha_api, store, notifier, status, clock) -> IdleBridge:
    """Abmelden (Spec TP7 3.3, die Integration wird entfernt): laufenden Boost auf den
    Wiederherstellungspunkt zuruecknehmen, alle Meldungen entfernen, dann Ruhezustand
    `abgemeldet`. Es wird kein Hilfssensor angelegt: fuer das Zuruecksetzen reichen Kurve, Offset
    und die Clamps. Scheitert es, wird es im Takt local_check_interval erneut versucht; der Status
    bleibt `abgemeldet` mit Grund."""
    try:
        effective = config.resolve_effective_options(options)
    except config.ConfigError as error:
        logger.error("Abmelden ohne Zuruecksetzen, Konfiguration ungueltig: %s", _without_credentials(str(error), options))
        restorer = None
    else:
        manifest = ChannelManifest(entity_ids={role: effective[f"entity_{role}"] for role in OVERRIDE_ROLES})
        restorer = Override(store, manifest, ha_api, effective)
    restored = restorer is not None and restorer.restore_and_clear(always_restore=False)
    notifier.clear_all()
    retry_seconds = config.local_check_interval(options)
    if restorer is None:
        grund = SIGN_OFF_INVALID_CONFIG
    elif restored:
        grund = None
    else:
        grund = SIGN_OFF_RESTORE_FAILED.format(seconds=retry_seconds)
    status.update(abgemeldet=True, grund=grund)
    bridge = _idle(clock, status, STATUS_ABGEMELDET)
    if restorer is not None and not restored:
        def _retry(event: Event) -> None:
            if restorer.restore_and_clear(always_restore=False):
                status.update(grund=None)
                return
            bridge.worker.schedule(retry_seconds, Event(EV_RECHECK))

        bridge.worker.register(EV_RECHECK, _retry)
        bridge.worker.schedule(retry_seconds, Event(EV_RECHECK))
    return bridge


def _finish_at_start(rt: Runtime, clock) -> IdleBridge:
    """Abo-Frist beim Start schon abgelaufen (Spec TP7 3.2): laufende Boosts zuruecknehmen (nicht
    mit Boost-Werten liegen bleiben), dann Ruhezustand `abo_beendet`. Scheitert das
    Zuruecksetzen, meldet der notifier das einmal (T2-7), und es wird im Takt
    local_check_interval im Worker erneut versucht."""
    rt.status.update(abo_beendet=True)
    bridge = _idle(clock, rt.status, STATUS_ABO_BEENDET)
    retry_seconds = config.local_check_interval(rt.options)

    def _attempt(event: Event | None = None) -> None:
        if abo.finish_grace(rt, always_restore=False, final_notice=False):
            abo.report_restore(rt.notifier, ok=True)
            rt.status.update(grund=None)
            return
        abo.report_restore(rt.notifier, ok=False)
        rt.status.update(grund=abo.RESTORE_FAILED_REASON)
        logger.error(
            "Abo-inaktiv-Frist abgelaufen, Boost-Werte konnten nicht zurueckgesetzt werden - "
            "erneuter Versuch in %s s", retry_seconds,
        )
        bridge.worker.schedule(retry_seconds, Event(EV_RECHECK))

    bridge.worker.register(EV_RECHECK, _attempt)
    _attempt()
    return bridge


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


def _on_health(rt: Runtime, event: Event) -> None:
    """Batterien, einzelne Raumfuehler (Spec TP6 3.5) und manuelle Eingriffe an Kurve/Offset
    (Spec TP7 3.6). Eigener Zeitplaneintrag: der lokale Check laeuft seit den eventgetriebenen
    Triggern nur auf Ereignisse."""
    rt.worker.schedule(config.local_check_interval(rt.options), Event(EV_HEALTH))
    if rt.store.state.abo_finished:
        return
    for check in (battery.check_batteries, room_sensors.check_room_sensors, manual_override.check_manual_override):
        try:
            check(rt)
        except Exception:
            logger.exception("Fehler in der Ueberwachung (%s), naechster Versuch im naechsten Takt", check.__name__)


def _on_mqtt_connected(rt: Runtime, event: Event) -> None:
    """Der erste Connect schliesst den Start ab (Status regelt); jeder Connect hebt eine
    abgelehnte Anmeldung auf."""
    if rt.status.flags.zugang_abgelehnt:
        rt.status.update(zugang_abgelehnt=False, grund=None, gestartet=True)
    elif not rt.status.flags.gestartet:
        rt.status.update(gestartet=True)
    rt.notifier.notify("zugang", STATE_OK, abo.ACCESS_OK_MESSAGE, critical=True)
    ticks.deliver(rt, delivery.MqttConnected())


def _on_ha_connected(rt: Runtime, event: Event) -> None:
    """Voller Status und HA-Benachrichtigungen bei jedem (Wieder-)Verbinden: nach einem
    HA-Neustart fehlen die Benachrichtigungen, und die Integration hat den Status nicht."""
    rt.status.publish()
    rt.notifier.republish_persistent()


def _on_heartbeat(rt: Runtime, event: Event) -> None:
    rt.worker.schedule(HEARTBEAT_SECONDS, Event(EV_HEARTBEAT))
    rt.status.publish()


def _unless_idle(rt: Runtime, handler, event: Event) -> None:
    """Im Ruhezustand (Fristende im Betrieb) laufen alle Handler ausser dem Lebenszeichen leer;
    sie planen sich dann auch nicht neu ein."""
    if not rt.idle:
        handler(rt, event)


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
        EV_HEALTH: _on_health,
    }
    for kind, handler in handlers.items():
        rt.worker.register(kind, functools.partial(_unless_idle, rt, handler))
    rt.worker.register(EV_HEARTBEAT, functools.partial(_on_heartbeat, rt))
    rt.worker.after_each(rt.status.publish_if_changed)


# --- Boot ---

def _prime(rt: Runtime) -> None:
    """Erster lokaler Check synchron vor mqtt.loop_start(): die Boost-Flags sind aus echten
    Sensorwerten bestimmt, bevor eine Server-Antwort verarbeitet wird (sie entscheiden, ob
    Serverwerte geschrieben oder nur gespeichert werden). Persistierte Boosts laufen weiter und
    enden regulaer ueber ihre Schwellen (N1). Scheitert der Check, bleiben die Flags stehen; der
    naechste erfolgreiche Check beendet die Boosts inklusive Zurueckschreiben (N4)."""
    try:
        rt.store.update(stable_target=regulation.read_room_target_live(rt))
        logger.info("Stable-Target-Cache initial befuellt (Boot-Priming): room_target=%s", rt.store.state.stable_target)
        regulation.run_local_check(rt)
    except Exception:
        logger.exception("Fehler beim initialen lokalen Check vor MQTT-Start, wird beim naechsten Ereignis erneut versucht")


def _start_bridge(options: dict, ha_api, clock=time.monotonic) -> Runtime | IdleBridge:
    """Gibt den gestarteten Laufzeit-Kontext zurueck oder den Ruhezustand, wenn gar nicht erst
    geregelt wird: nicht eingerichtet, abgemeldet, Konfigurationsfehler, Abo-Frist abgeschlossen."""
    if not config.is_configured(options):
        logger.info(
            "Add-on ist noch nicht eingerichtet -- bitte die SmartHeat-Integration in "
            "Home Assistant installieren und dort die Verbindung zu diesem Add-on "
            "einrichten (sie schreibt die Konfiguration automatisch per Supervisor-API und "
            "startet das Add-on danach neu). Bis dahin bleibt das Add-on im Ruhezustand."
        )
        return _idle(clock, None, IDLE_NOT_CONFIGURED)
    # Erst warten, bis HA antwortet: Status und Meldungen sollen ankommen, und ohne HA geht
    # ohnehin nichts (Hilfs-Entities, Anlage).
    _wait_until_reachable(ha_api)
    store = StateStore(config.BACKUP_PATH, config.FAILSAFE_PATH)
    notifier = Notifier(store, ha_api, config.notify_services(options), config.notify_hints_off(options))
    ticks.seed_notices(notifier, store.state.delivery)
    status = StatusReporter(ha_api, options["tenant_id"], options.get("setup_id"), store)
    status.publish()
    if config.is_signed_off(options):
        return _sign_off(options, ha_api, store, notifier, status, clock)
    try:
        options = config.resolve_effective_options(options)
        error = config.validate(options)
        if error:
            raise config.ConfigError(error)
        _wait_for_required_entities(ha_api, options)
        derived = _ensure_derived_sensors_with_retry(ha_api, options)
        manifest = build_manifest(options, derived.entity_ids)
    except StartupError as error:
        return _fail_start(notifier, status, clock, str(error), error.key)
    except (config.ConfigError, ManifestError) as error:
        # Pruefungstexte sind deterministisch: der Text ist zugleich die Identitaet.
        return _fail_start(notifier, status, clock, _without_credentials(str(error), options))

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
        status.update(abo=ABO_AKTIV)
    elif abo_status == entitlement.INACTIVE:
        inactive_since = entitlement.load_inactive_since(config.ENTITLEMENT_PATH)
        if inactive_since is not None and entitlement.grace_expired(inactive_since, now):
            return _finish_at_start(rt, clock)
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
    for kind in (EV_WATCHDOG, EV_TELEMETRY, EV_DAYNIGHT, EV_GRACE_CHECK, EV_HEALTH):
        rt.worker.schedule(0, Event(kind))
    rt.worker.schedule(HEARTBEAT_SECONDS, Event(EV_HEARTBEAT))
    if abo_inactive:
        # Ohne MQTT kein EV_MQTT_CONNECTED: die lokale Regelung laeuft, der Start ist abgeschlossen.
        status.update(gestartet=True)
    else:
        # Offenen Tick aus failsafe_state.json sofort mit derselben seq erneut versuchen.
        ticks.deliver(rt, delivery.Boot())
    status.publish_if_changed()
    return rt


def _run_bridge(options: dict, ha_api):
    """Laeuft, bis der Prozess beendet wird. Voruebergehende Fehler (HA nicht bereit, Broker nicht
    erreichbar, Server schweigt) und Endzustaende beenden das Add-on nie."""
    return _start_bridge(options, ha_api).worker.run()


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    options = config.load_options_safe(config.OPTIONS_PATH)
    ha_api = HomeAssistantApi(base_url="http://supervisor", token=os.environ["SUPERVISOR_TOKEN"])

    _run_bridge(options, ha_api)


if __name__ == "__main__":
    main()
