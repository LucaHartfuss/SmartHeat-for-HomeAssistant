"""ClientApp (Spec SHG 3.3): Start, Handler des Regel-Workers, Ruhezustand und Abmelden, fuer jeden Host gleich.

Alle Regelungsereignisse laufen nacheinander im Hauptthread (RegulationWorker). Der Client beendet sich nie absichtlich:
die Restart-Policy des Hosts (Supervisor-Watchdog bzw. Container) wuerde auch einen sauberen Exit 0 neu starten
(Spec TP7). Endzustaende sind ein Ruhezustand, in dem nur noch das Lebenszeichen laeuft. Was hostspezifisch ist
(Warten auf HA, Optionen, Hilfs-Entities, Bindings, Trigger), liefert der Host ueber das Protokoll Host."""
import functools
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from smartheat_core import derived, enforce, wallclock, write_budget
from smartheat_core.binding import PlantBinding
from smartheat_core.pipeline import LeverPipeline, WriteBudgetExhausted
from smartheat_core.safety import LocalSafety
from smartheat_runtime import (
    abo,
    battery,
    datentraeger,
    delivery,
    entitlement,
    mqtt_link,
    regulation,
    room_sensors,
    telemetry,
    ticks,
    waerme_hint,
)
from smartheat_runtime.delivery import ROLE_DATENTRAEGER, SOURCE_LOCAL, DataFault
from smartheat_runtime.notifier import STATE_OK, Notifier
from smartheat_runtime.ports import NotifySink, SignalSource, StatusSink, TriggerSource
from smartheat_runtime.roles import ChannelManifest
from smartheat_runtime.runtime import (
    EV_ACK_TIMEOUT,
    EV_AUTH_REJECTED,
    EV_CONNECTION_CHECK,
    EV_GRACE_CHECK,
    EV_HEALTH,
    EV_HEARTBEAT,
    EV_LOCAL_CHECK,
    EV_MQTT_CONNECTED,
    EV_RECHECK,
    EV_RETRY_DUE,
    EV_SETPOINTS,
    EV_SOURCE_CONNECTED,
    EV_TELEMETRY,
    EV_WATCHDOG,
    Runtime,
)
from smartheat_runtime.runtime_config import BootInfo, RuntimeConfig
from smartheat_runtime.state import StateStore, StorageError
from smartheat_runtime.status import (
    ABO_AKTIV,
    HEARTBEAT_SECONDS,
    STATUS_ABGEMELDET,
    STATUS_ABO_BEENDET,
    STATUS_KONFIGURATIONSFEHLER,
    StatusReporter,
)
from smartheat_runtime.texts import HostTexts
from smartheat_runtime.worker import Event, RegulationWorker

# Im Konfigurationsfehler prueft ein frischer Prozess nach dieser Zeit erneut (z. B. eine spaet
# geladene Integration); der persistierte Meldezustand verhindert eine Wiederholungsmeldung.
CONFIG_RECHECK_SECONDS = 900
# Verbindungswaechter (TP12b, AU-033): so lange ohne MQTT-Verbindung und ohne offenen Tick, dann
# klaert ein Pruef-Tick den Server (Notbetrieb ueber dessen Ack-Timeouts). Kurze Abrisse des
# cloudflared-Tunnels bleiben darunter.
CONNECTION_LOSS_PROBE_SECONDS = 900
IDLE_NOT_CONFIGURED = "nicht_eingerichtet"
CONFIG_OK_MESSAGE = "SmartHeat: Einrichtung in Ordnung, die Heizungssteuerung läuft."
CONFIG_ERROR_MESSAGE = (
    "SmartHeat: Konfigurationsfehler – {grund}. Die Heizungssteuerung ist gestoppt, "
    "die Anlage behält ihre letzten Werte."
)
CONFIG_ERROR_BOOST_MESSAGE = (
    "SmartHeat: Konfigurationsfehler – {grund}. Die Heizungssteuerung ist gestoppt, die Anlage steht aber noch auf "
    "Boost-Werten. Bitte die zuletzt gelernten Werte von Hand einstellen ({werte})."
)
SIGN_OFF_INVALID_CONFIG = "Konfiguration ungültig, die Anlage wurde nicht zurückgesetzt"
SIGN_OFF_RESTORE_FAILED = "Zurücksetzen der Anlage scheitert, neuer Versuch in {seconds} s"
SIGN_OFF_RESTORE_FAILED_MESSAGE = (
    "SmartHeat wurde entfernt, konnte die Heizung aber nicht auf die zuletzt gelernten Werte "
    "zurücksetzen ({werte}). Es wird weiter versucht, sonst bitte diese Werte von Hand einstellen."
)
SIGN_OFF_NOT_RESTORED_MESSAGE = (
    "SmartHeat wurde entfernt, die Heizung steht aber noch auf Boost-Werten (Konfiguration "
    "ungültig, kein Zurücksetzen möglich). Bitte die zuletzt gelernten Werte von Hand einstellen ({werte})."
)

logger = logging.getLogger(__name__)


class StartFailure(Exception):
    """Konfigurations- oder Startfehler des Hosts: Ruhezustand `konfigurationsfehler`. `key` ist die stabile Identitaet
    fuer den Meldezustand (None = der Text selbst), siehe _fail_start."""

    def __init__(self, grund: str, key: str | None = None) -> None:
        super().__init__(grund)
        self.grund = grund
        self.key = key


@dataclass(frozen=True)
class Notice:
    """Nicht kritischer Hinweis des Hosts beim Start (HA: Quelle der Raum- oder Aussentemperatur gewechselt)."""
    key: str
    state: str
    message: str


@dataclass(frozen=True)
class Loaded:
    config: RuntimeConfig
    manifest: ChannelManifest
    binding: PlantBinding
    notices: tuple[Notice, ...] = ()


@dataclass(frozen=True)
class RestoreParts:
    """Was das Abmelden zum Zuruecksetzen braucht."""
    binding: PlantBinding
    safety: LocalSafety


class Host(Protocol):
    """Was ein Host der Laufzeit liefert (Spec SHG 3.2). Alle Aufrufe laufen im Hauptthread vor worker.run()."""

    signals: SignalSource
    status_sink: StatusSink
    notify_sink: NotifySink
    texts: HostTexts

    def boot_info(self) -> BootInfo: ...

    def wait_until_ready(self) -> None:
        """Blockiert, bis die Quelle antwortet (HA: GET /api/config meldet RUNNING); ohne Budget."""
        ...

    def sign_off_parts(self) -> RestoreParts | None:
        """Binding und Sicherheitswerte zum Zuruecksetzen; None bei ungueltiger Konfiguration."""
        ...

    def load(self) -> Loaded:
        """Konfiguration pruefen, Signale und Anlage vorbereiten; wirft StartFailure."""
        ...

    def trigger_source(self, manifest: ChannelManifest, worker: RegulationWorker) -> TriggerSource: ...


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


def _fail_start(notifier, status, clock, grund: str, key: str | None = None, *, store=None,
                parts: RestoreParts | None = None) -> IdleBridge:
    """Meldezustand `fehler:<key>` (stabile Identitaet, Standard: der Text selbst); Meldung und
    Status tragen den ausfuehrlichen Grund. Derselbe Fehler wie beim letzten Start pusht nicht
    erneut, legt aber die offene Meldung neu an (in HA fehlt sie nach einem Host-Neustart). Danach
    Ruhezustand mit Neupruefung nach CONFIG_RECHECK_SECONDS. Laeuft ein Boost, nimmt der Start ihn ueber `parts`
    zurueck; geht das nicht, sagt die Meldung, dass die Boost-Werte noch auf der Anlage stehen (Audit 4, A4-10)."""
    logger.error("FEHLER: %s", grund)
    message = CONFIG_ERROR_MESSAGE.format(grund=grund)
    boosting = store is not None and (store.state.boost_active or store.state.emergency_boost_active)
    if boosting and not _end_boosts(store, parts, clock):
        message = CONFIG_ERROR_BOOST_MESSAGE.format(grund=grund, werte=_restore_values_text(store.state))
    if not notifier.notify("konfiguration", f"fehler:{grund if key is None else key}", message, critical=True):
        notifier.refresh_persistent("konfiguration", message)
    status.update(konfigurationsfehler=True, grund=grund)
    return _idle(clock, status, STATUS_KONFIGURATIONSFEHLER, recheck_after=CONFIG_RECHECK_SECONDS)


def _end_boosts(store, parts: RestoreParts | None, clock) -> bool:
    """Laufende Boosts auf den Wiederherstellungspunkt zuruecknehmen; True, wenn danach keiner mehr laeuft."""
    if parts is None:
        return False
    try:
        LeverPipeline(store, parts.binding, parts.safety, clock=clock).set_boosts(comfort=False, emergency=False)
    except Exception:
        logger.exception("Boost beim Konfigurationsfehler nicht zurueckgenommen")
        return False
    return not (store.state.boost_active or store.state.emergency_boost_active)


def _sign_off(parts: RestoreParts | None, store, notifier, status, clock, retry_seconds: float) -> IdleBridge:
    """Abmelden (Spec TP7 3.3, die Integration bzw. das Portal entfernt den Client): laufenden Boost auf den
    Wiederherstellungspunkt zuruecknehmen, alle Meldungen entfernen, dann Ruhezustand `abgemeldet`. Scheitert es, wird
    es im Takt retry_seconds erneut versucht; der Status bleibt `abgemeldet` mit Grund."""
    restorer = None if parts is None else LeverPipeline(store, parts.binding, parts.safety, clock=clock)
    boosting = store.state.boost_active or store.state.emergency_boost_active
    restored = restorer is not None and restorer.restore_and_clear(always_restore=False)
    # Die Meldung zum Zuruecksetzen bleibt: nach dem Entfernen ist sie der einzige Hinweis, dass
    # die Anlage noch auf Boost-Werten steht (TP7-Gates 2026-09-29).
    notifier.clear_all(keep=(abo.RESTORE_KEY,))
    if restorer is None:
        grund = SIGN_OFF_INVALID_CONFIG
        if boosting:
            _report_sign_off_restore(notifier, SIGN_OFF_NOT_RESTORED_MESSAGE, store.state)
    elif restored:
        grund = None
        abo.report_restore(notifier, ok=True)
    else:
        grund = SIGN_OFF_RESTORE_FAILED.format(seconds=retry_seconds)
        _report_sign_off_restore(notifier, SIGN_OFF_RESTORE_FAILED_MESSAGE, store.state)
    status.update(abgemeldet=True, grund=grund)
    bridge = _idle(clock, status, STATUS_ABGEMELDET)
    if restorer is not None and not restored:
        def _retry(event: Event) -> None:
            if restorer.restore_and_clear(always_restore=False):
                abo.report_restore(notifier, ok=True)
                status.update(grund=None)
                return
            bridge.worker.schedule(retry_seconds, Event(EV_RECHECK))

        bridge.worker.register(EV_RECHECK, _retry)
        bridge.worker.schedule(retry_seconds, Event(EV_RECHECK))
    return bridge


def _hint(notifier, key: str, state: str, message: str) -> None:
    """Nicht kritischer Hinweis der Hebel-Pipeline (Plan 3b: Tageslimit, Lebensdauerzaehler)."""
    notifier.notify(key, state, message, critical=False)


def _report_sign_off_restore(notifier, template: str, state) -> None:
    """Kritische Meldung mit den Werten des Wiederherstellungspunkts, einmal mit Push; bei jedem
    weiteren Start (Neustart, Zuruecksetzen scheitert weiter) nur die offene Meldung neu, die der
    Host (HA) nicht speichert."""
    message = template.format(werte=_restore_values_text(state))
    if not notifier.notify(abo.RESTORE_KEY, abo.RESTORE_FAILED_STATE, message, critical=True):
        notifier.refresh_persistent(abo.RESTORE_KEY, message)


def _restore_values_text(state) -> str:
    parts = [
        f"{label} {value:g}".replace(".", ",")
        for label, value in (
            ("Kurve", state.restore_point.get("curve")), ("Parallelverschiebung", state.restore_point.get("room_setpoint")),
        )
        if value is not None
    ]
    return ", ".join(parts) if parts else "Werte unbekannt"


def _finish_at_start(rt: Runtime, clock) -> IdleBridge:
    """Abo-Frist beim Start schon abgelaufen (Spec TP7 3.2): laufende Boosts zuruecknehmen (nicht
    mit Boost-Werten liegen bleiben), dann Ruhezustand `abo_beendet`. Scheitert das
    Zuruecksetzen, meldet der notifier das einmal (T2-7), und es wird im Takt
    local_check_interval im Worker erneut versucht."""
    status = rt.status
    assert status is not None  # beim Boot gesetzt
    status.update(abo_beendet=True)
    bridge = _idle(clock, status, STATUS_ABO_BEENDET)
    retry_seconds = rt.config.local_check_interval

    def _attempt(event: Event | None = None) -> None:
        if abo.finish_grace(rt, always_restore=False, final_notice=False):
            abo.report_restore(rt.notifier, ok=True)
            status.update(grund=None)
            return
        abo.report_restore(rt.notifier, ok=False)
        status.update(grund=abo.RESTORE_FAILED_REASON)
        logger.error(
            "Abo-inaktiv-Frist abgelaufen, Boost-Werte konnten nicht zurueckgesetzt werden - "
            "erneuter Versuch in %s s", retry_seconds,
        )
        bridge.worker.schedule(retry_seconds, Event(EV_RECHECK))

    bridge.worker.register(EV_RECHECK, _attempt)
    _attempt()
    return bridge


# --- Handler des Regel-Workers ---

def _on_local_check(rt: Runtime, event: Event) -> None:
    """Bei room_target_fired den Cache frisch lesen und den Mindestvorlauf nachfuehren, dann
    Boost-Logik, dann "Tick faellig?".
    Beides getrennt abgesichert: ein toter room_actual-Fuehler laesst den Boost-Teil scheitern,
    darf aber keinen Tick verhindern; dessen Versuch meldet den Fuehler als Datenfehler."""
    if rt.store.state.abo_finished:
        return
    if event.data.get("room_target_fired"):
        regulation.refresh_stable_target(rt)
        derived.sync(rt)
    if not rt.zone_prepared:
        _prepare_zone(rt)
    try:
        regulation.run_local_check(rt)
    except Exception:
        logger.exception("Fehler im lokalen Check (Boost/Notfall-Boost), wird beim naechsten Ereignis erneut versucht")
    try:
        trigger = regulation.claim_due_tick(rt, wallclock.now())
    except StorageError:
        logger.warning(
            "Tick nicht gebucht, Datentraeger nicht beschreibbar (N5) - naechster Versuch beim naechsten Anlass"
        )
        return
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
    """Fallback bei getrenntem Trigger-Eingang: Cache frisch (ohne Entprellung) lesen, dann
    lokaler Check. Bei verbundenem Trigger-Client passiert nichts."""
    rt.worker.schedule(rt.config.local_check_interval, Event(EV_WATCHDOG))
    if rt.trigger_client is None or not rt.trigger_client.connected:
        rt.worker.post_coalesced(EV_LOCAL_CHECK, room_target_fired=True)


def _on_connection_check(rt: Runtime, event: Event) -> None:
    """Verbindungswaechter (Spec TP12b 1.1): ohne offenen Tick gibt es keinen Zustellversuch und
    damit nie einen Notbetrieb. Fehlt die Verbindung CONNECTION_LOSS_PROBE_SECONDS am Stueck, legt er
    deshalb einen Pruef-Tick an; das Ack nach dem Wiederverbinden beendet den Notbetrieb regulaer."""
    rt.worker.schedule(rt.config.local_check_interval, Event(EV_CONNECTION_CHECK))
    state = rt.store.state
    if rt.mqtt_client is None or state.abo_inactive_since is not None or rt.mqtt_client.is_connected():
        rt.mqtt_down_since = None
        return
    now = rt.clock()
    if rt.mqtt_down_since is None:
        rt.mqtt_down_since = now
        return
    if (now - rt.mqtt_down_since >= CONNECTION_LOSS_PROBE_SECONDS
            and rt.mqtt_client.connect_failures >= abo.CONNECT_FAILURES_BEFORE_STATUS_QUERY):
        abo.handle_connection_failing(rt)
        if rt.store.state.abo_inactive_since is not None:
            return  # eindeutig inaktiv: kein Pruef-Tick mehr
    if now - rt.mqtt_down_since < CONNECTION_LOSS_PROBE_SECONDS or state.delivery.pending is not None:
        return
    ticks.start_probe_tick(rt, f"seit {now - rt.mqtt_down_since:.0f} s keine MQTT-Verbindung")


def _on_telemetry(rt: Runtime, event: Event) -> None:
    rt.worker.schedule(rt.config.telemetry_interval, Event(EV_TELEMETRY))
    state = rt.store.state
    if state.abo_inactive_since is not None or rt.mqtt_client is None:
        return
    if not rt.mqtt_client.is_connected():
        # Ohne Verbindung nicht publizieren (AU-035): paho staute die Nachrichten unbegrenzt. Der
        # Server erkennt die Luecke ueber MAX_GAP.
        logger.debug("Telemetrie uebersprungen, keine MQTT-Verbindung")
        return
    datenfehler = state.delivery.datenfehler
    if datenfehler is None and rt.store.storage_failed:
        datenfehler = DataFault(SOURCE_LOCAL, (ROLE_DATENTRAEGER,))
    telemetry.run_telemetry_tick(
        rt.manifest, rt.signals, rt.mqtt_client,
        boost_active=state.boost_active, failsafe_active=state.delivery.notbetrieb,
        datenfehler=datenfehler, room_target=state.stable_target,
        waerme=lambda room, kpi, regulation: waerme_hint.apply_tick(rt, room, kpi, regulation),
        energy=telemetry.energy_normalizer(rt.store, rt.override.binding.description.energy_counters),
    )


def _on_grace_check(rt: Runtime, event: Event) -> None:
    rt.worker.schedule(rt.config.local_check_interval, Event(EV_GRACE_CHECK))
    abo.check_grace_end(rt)


def _on_health(rt: Runtime, event: Event) -> None:
    """Batterien, einzelne Raumfuehler (Spec TP6 3.5) und manuelle Eingriffe an Steigung,
    Parallelverschiebung, Mindestvorlauf und Zonen-Betriebsart (Durchsetzung, TP11). Eigener
    Zeitplaneintrag: der lokale Check laeuft seit den eventgetriebenen Triggern nur auf Ereignisse."""
    rt.worker.schedule(rt.config.local_check_interval, Event(EV_HEALTH))
    try:
        rt.store.flush()
    except StorageError as error:
        logger.warning("Datentraeger weiterhin nicht beschreibbar: %s", error)
    if rt.store.state.abo_finished:
        return
    for check in (battery.check_batteries, room_sensors.check_room_sensors, enforce.check_manual_override):
        try:
            check(rt)
        except Exception:
            logger.exception("Fehler in der Ueberwachung (%s), naechster Versuch im naechsten Takt", check.__name__)


def _on_mqtt_connected(rt: Runtime, event: Event) -> None:
    """Der erste Connect schliesst den Start ab (Status regelt); jeder Connect hebt eine
    abgelehnte Anmeldung auf."""
    status = rt.status
    assert status is not None  # beim Boot gesetzt
    rt.auth_rejected_queried_at = None
    rt.auth_rejected_last_status = None
    rt.connection_failing_queried_at = None
    if status.flags.zugang_abgelehnt:
        status.update(zugang_abgelehnt=False, grund=None, gestartet=True)
    elif not status.flags.gestartet:
        status.update(gestartet=True)
    rt.notifier.notify("zugang", STATE_OK, abo.ACCESS_OK_MESSAGE, critical=True)
    ticks.deliver(rt, delivery.MqttConnected())


def _on_source_connected(rt: Runtime, event: Event) -> None:
    """Voller Status und offene Meldungen bei jedem (Wieder-)Verbinden der Quelle: nach einem
    Neustart der Quelle (HA) fehlen die offenen Meldungen, und der Empfaenger des Status hat ihn nicht."""
    status = rt.status
    assert status is not None  # beim Boot gesetzt
    status.publish()
    rt.notifier.republish_persistent()


def _on_heartbeat(rt: Runtime, event: Event) -> None:
    rt.worker.schedule(HEARTBEAT_SECONDS, Event(EV_HEARTBEAT))
    status = rt.status
    assert status is not None  # beim Boot gesetzt
    status.publish()


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
        EV_SOURCE_CONNECTED: _on_source_connected,
        EV_WATCHDOG: _on_watchdog,
        EV_TELEMETRY: _on_telemetry,
        EV_GRACE_CHECK: _on_grace_check,
        EV_HEALTH: _on_health,
        EV_CONNECTION_CHECK: _on_connection_check,
    }
    for kind, handler in handlers.items():
        rt.worker.register(kind, functools.partial(_unless_idle, rt, handler))
    rt.worker.register(EV_HEARTBEAT, functools.partial(_on_heartbeat, rt))
    rt.worker.after_each(functools.partial(_after_each, rt))


def _after_each(rt: Runtime) -> None:
    """Nach jedem Worker-Ereignis: Datentraeger-Meldung (TP12b), dann das Status-Event, falls es
    sich geaendert hat."""
    try:
        datentraeger.report(rt.store, rt.notifier, rt.texts)
    except Exception:
        logger.exception("Datentraeger-Meldung fehlgeschlagen")
    status = rt.status
    assert status is not None  # beim Boot gesetzt
    status.publish_if_changed()


# --- Boot ---

def _prime(rt: Runtime) -> None:
    """Erster lokaler Check synchron vor mqtt.loop_start(): Stable-Target-Cache fuellen, beim
    ersten Start ohne gespeicherte Parallelverschiebung den Steigungs-Punkt neu setzen und einen
    Tick erzwingen (_first_start), Zone auf Manuell mit brauchbarer Parallelverschiebung (vor dem
    ersten Snapshot, Plan-Praezisierung 11), Mindestvorlauf = Raum-Soll, dann Boost-Flags aus
    echten Sensorwerten. Persistierte Boosts laufen weiter und enden regulaer ueber ihre Schwellen
    (N1). Der Ursprungswert der Heizgrenze wird vor dem ersten eigenen Schreiben gemerkt (TP12h).
    Scheitert ein Schritt, laufen die uebrigen trotzdem."""
    try:
        rt.override.capture_originals()
    except Exception:
        logger.exception(
            "Ursprungswert der Heizgrenze beim Start nicht gespeichert, wird beim ersten Schreiben erneut versucht"
        )
    try:
        rt.store.update(stable_target=regulation.read_room_target_live(rt))
        logger.info("Stable-Target-Cache initial befuellt (Boot-Priming): room_target=%s", rt.store.state.stable_target)
    except Exception:
        logger.exception("room_target beim Start nicht lesbar, wird beim naechsten Ereignis erneut versucht")
    if rt.store.state.restore_point.get("room_setpoint") is None:
        _first_start(rt)
    _prepare_zone(rt)
    derived.sync(rt)
    try:
        regulation.run_local_check(rt)
    except Exception:
        logger.exception("Fehler beim initialen lokalen Check vor MQTT-Start, wird beim naechsten Ereignis erneut versucht")


def _first_start(rt: Runtime) -> None:
    """Erster Start ohne gespeicherte Parallelverschiebung (0.23.0 -> 0.24.0): Steigungs-Punkt
    vom Anlagenwert neu setzen und sofort einen Tick erzwingen, damit der Erstkontakt des Servers
    mit den Istwerten startet statt erst beim naechsten Tagestick oder Sollwertwechsel."""
    try:
        rt.override.reseed_from_plant("curve")
    except Exception:
        logger.exception("Wiederherstellungspunkt der Steigung beim ersten Start nicht gespeichert")
    try:
        rt.store.update_saved(last_published_target_rt=None)
    except Exception:
        logger.exception("Sofortiger Tick beim ersten Start nicht gebucht, es gilt der naechste regulaere Anlass")


def _may_prepare_zone(rt: Runtime) -> bool:
    """Wiederholungs-Kontingent (write_budget.RETRY_QUOTA): nach einem Fehlschlag hoechstens ein
    Versuch pro 30 min und 6 Fehlschlaege am Tag. Jeder Versuch kann zwei Cloud-Aufrufe kosten; bei
    403 "Quota Exceeded" wuerde ein Versuch in jedem lokalen Check die Sperre verlaengern. Ein
    gelungener Versuch kostet nichts (er laeuft bei jedem Start)."""
    key, day = write_budget.ZONE_PREPARE, write_budget.today()
    entry = write_budget.get(rt.store, key)
    if write_budget.allowed(entry, write_budget.RETRY_QUOTA, rt.clock(), day):
        return True
    if write_budget.limit_first_reached(entry, write_budget.RETRY_QUOTA, day):
        assert entry is not None
        logger.warning(
            "Zone: Tageslimit von %d Vorbereitungsversuchen erreicht, naechster Versuch morgen",
            write_budget.MAX_PER_DAY,
        )
        write_budget.put(rt.store, key, {**entry, "limit_notified": day})
    return False


def _prepare_zone(rt: Runtime) -> None:
    """Zone vorbereiten (LeverPipeline.prepare_start); scheitert es, versucht es ein spaeterer lokaler
    Check erneut (Kontingent: _may_prepare_zone), statt dass erst die Durchsetzung nach
    settle_seconds des Bindings umstellt."""
    if not _may_prepare_zone(rt):
        return
    try:
        rt.override.prepare_start(rt.store.state.stable_target)
    except WriteBudgetExhausted as error:
        # Plan 3b (Weishaupt): Tagesbudget erreicht, kein Fehlschlag der Anlage -- kein Versuch im Kontingent, der
        # naechste lokale Check (spaetestens am naechsten Tag) versucht es erneut; den Hinweis gibt die Pipeline.
        logger.info("Zone nicht vorbereitet: %s, naechster Versuch beim naechsten lokalen Check", error)
        return
    except Exception:
        write_budget.record_attempt(rt.store, write_budget.ZONE_PREPARE, rt.clock())
        logger.exception("Zone konnte nicht vorbereitet werden, naechster Versuch beim naechsten lokalen Check")
        return
    write_budget.record_success(rt.store, write_budget.ZONE_PREPARE)
    rt.zone_prepared = True


def start(host: Host, clock: Callable[[], float] = time.monotonic) -> Runtime | IdleBridge:
    """Gibt den gestarteten Laufzeit-Kontext zurueck oder den Ruhezustand, wenn gar nicht erst geregelt wird: nicht
    eingerichtet, abgemeldet, Konfigurationsfehler, Abo-Frist abgeschlossen."""
    boot = host.boot_info()
    if not boot.signed_off and not boot.configured:
        return _idle(clock, None, IDLE_NOT_CONFIGURED)
    # Erst warten, bis die Quelle antwortet: Status und Meldungen sollen ankommen, und ohne sie geht ohnehin nichts.
    host.wait_until_ready()
    assert boot.tenant_id is not None  # configured bzw. signed_off setzen ihn voraus
    store = StateStore(boot.backup_path, boot.failsafe_path)
    notifier = Notifier(store, host.notify_sink, boot.notify_hints_off)
    ticks.seed_notices(notifier, store.state.delivery)
    status = StatusReporter(host.status_sink, boot.tenant_id, boot.setup_id, store, boot.lever_set, boot.client_version)
    status.publish()
    if boot.signed_off:
        return _sign_off(host.sign_off_parts(), store, notifier, status, clock, boot.local_check_interval)
    try:
        loaded = host.load()
    except StartFailure as error:
        boosting = store.state.boost_active or store.state.emergency_boost_active
        return _fail_start(notifier, status, clock, error.grund, error.key, store=store,
                           parts=host.sign_off_parts() if boosting else None)

    notifier.notify("konfiguration", STATE_OK, CONFIG_OK_MESSAGE, critical=True)
    for notice in loaded.notices:
        notifier.notify(notice.key, notice.state, notice.message, critical=False)
    config = loaded.config
    rt = Runtime(
        manifest=loaded.manifest, signals=host.signals, config=config,
        worker=RegulationWorker(clock=clock), store=store,
        override=LeverPipeline(
            store, loaded.binding, config.local_safety, clock=clock, notify=functools.partial(_hint, notifier),
        ),
        notifier=notifier, status=status, clock=clock, texts=host.texts,
    )
    # Abo-Status erst hier: Abschluss-Start und lokaler Modus brauchen Manifest und Clamps.
    # "unknown" (accounts-api nicht erreichbar) startet normal -- fail-open.
    now = wallclock.now()
    abo_status = entitlement.query(config)
    if abo_status == entitlement.ACTIVE:
        entitlement.clear(config.entitlement_path)
        notifier.notify("abo", STATE_OK, abo.ABO_ACTIVE_MESSAGE, critical=True)
        status.update(abo=ABO_AKTIV)
    elif abo_status == entitlement.INACTIVE:
        inactive_since = entitlement.load_inactive_since(config.entitlement_path)
        if inactive_since is not None and entitlement.grace_expired(inactive_since, now):
            return _finish_at_start(rt, clock)
        abo.enter_inactive(rt, now)

    _register_handlers(rt)
    abo_inactive = rt.store.state.abo_inactive_since is not None
    if not abo_inactive:
        rt.mqtt_client = mqtt_link.create_mqtt_client(config, rt.worker)

    _prime(rt)
    if rt.mqtt_client is not None:
        rt.mqtt_client.loop_start()

    rt.trigger_client = host.trigger_source(loaded.manifest, rt.worker)
    rt.trigger_client.start()
    for kind in (EV_WATCHDOG, EV_TELEMETRY, EV_GRACE_CHECK, EV_HEALTH, EV_CONNECTION_CHECK):
        rt.worker.schedule(0, Event(kind))
    rt.worker.schedule(HEARTBEAT_SECONDS, Event(EV_HEARTBEAT))
    if abo_inactive:
        # Ohne MQTT kein EV_MQTT_CONNECTED: die lokale Regelung laeuft, der Start ist abgeschlossen.
        status.update(gestartet=True)
    else:
        # Offenen Tick aus failsafe_state.json sofort mit derselben seq erneut versuchen.
        ticks.deliver(rt, delivery.Boot())
        _resolve_stale_notbetrieb(rt, abo_status)
    status.publish_if_changed()
    return rt


def _resolve_stale_notbetrieb(rt: Runtime, abo_status: str) -> None:
    """Notbetrieb ohne offenen Tick endet nie von selbst (Spec TP12b 1.2): im Betrieb hat er immer
    einen Tick, dessen Ack ihn beendet. Bei aktivem Abo still beenden -- nach dem stillen seed wurde
    er nie gemeldet, eine Entwarnung waere falsch --, in jedem Fall per Pruef-Tick klaeren."""
    state = rt.store.state.delivery
    if not state.notbetrieb or state.pending is not None:
        return
    if abo_status == entitlement.ACTIVE:
        ticks.deliver(rt, delivery.ClearStaleNotbetrieb())
        rt.notifier.notify(
            "notbetrieb", STATE_OK, delivery.notification_text(delivery.NOTIFY_NOTBETRIEB_OFF, (), {}, rt.texts),
            critical=True, silent_ok=True,
        )
    ticks.start_probe_tick(rt, "Notbetrieb ohne offenen Tick beim Start")
