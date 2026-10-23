"""Abo-inaktiv-Modus (Notbetrieb ohne Server bis zum Fristende) und Fristende."""
import logging
from dataclasses import replace
from datetime import datetime

from smartheat_core import wallclock
from smartheat_runtime import entitlement
from smartheat_runtime.notifier import STATE_OK
from smartheat_runtime.runtime import Runtime

logger = logging.getLogger(__name__)

ABO_ENDED_MESSAGE = (
    "SmartHeat: Abo seit 30 Tagen inaktiv. Die Heizungssteuerung ist beendet, "
    "die zuletzt gelernten Werte bleiben eingestellt."
)
ABO_ACTIVE_MESSAGE = "SmartHeat: Abo wieder aktiv, die Heizungssteuerung läuft wieder normal."
RESTORE_KEY = "wiederherstellung"
RESTORE_FAILED_STATE = "fehlgeschlagen"
RESTORE_FAILED_MESSAGE = "SmartHeat: Zurücksetzen auf die zuletzt gelernten Werte scheitert. Bitte die Anlage prüfen."
RESTORE_OK_MESSAGE = "SmartHeat: Die zuletzt gelernten Werte sind wieder eingestellt."
RESTORE_FAILED_REASON = "Zurücksetzen auf die zuletzt gelernten Werte scheitert"


def inactive_message(inactive_since: datetime) -> str:
    end = entitlement.grace_end(inactive_since).strftime("%d.%m.%Y")
    return (
        f"SmartHeat: Abo inaktiv. Die Heizung läuft noch bis {end} im Notbetrieb weiter, "
        f"danach bleiben die zuletzt gelernten Werte fest eingestellt."
    )


def enter_inactive(rt: Runtime, now: datetime) -> None:
    """Notbetrieb an (persistiert), Kanal des Geraetekerns gestoppt, keine Snapshots/Telemetrie mehr. Gemeldet
    wird nur beim erstmaligen Setzen von inactive_since, nicht bei jedem Neustart.
    `mqtt_client.stop()` schaltet den Kanal der Laufzeit ab (smartheat_device.laufzeit.RuntimeChannel nimmt keine
    Ereignisse mehr an); den Link betreibt weiter das Geraet."""
    state = rt.store.state
    if state.abo_inactive_since is not None:
        return
    try:
        since, newly_set = entitlement.mark_inactive(rt.config.entitlement_path, now)
    except Exception:
        # Ein Schreibfehler (Datentraeger) darf den Notbetrieb nicht verhindern.
        logger.exception(
            "Abo-inaktiv-Zeitpunkt konnte nicht gespeichert werden - Abo-inaktiv-Modus "
            "gilt nur bis zum naechsten Neustart, Frist ab jetzt"
        )
        since, newly_set = now, True
    rt.store.update(abo_inactive_since=since)
    rt.store.set_delivery(replace(state.delivery, pending=None, notbetrieb=True))
    if rt.mqtt_client is not None:
        try:
            rt.mqtt_client.stop()
        except Exception:
            logger.exception("MQTT-Verbindung konnte nicht sauber beendet werden")
    if newly_set:
        rt.notifier.notify("abo", "inaktiv", inactive_message(since), critical=True)
    else:
        logger.warning(
            "Abo weiterhin inaktiv (seit %s) - Notbetrieb laeuft bis %s.",
            since.strftime("%d.%m.%Y"), entitlement.grace_end(since).strftime("%d.%m.%Y"),
        )


def finish_grace(rt: Runtime, *, always_restore: bool, final_notice: bool) -> bool:
    """Fristende (always_restore, final_notice) bzw. Abschluss-Start (beides False): laufende
    Boosts beenden, zuletzt gelernte Werte auf die Anlage. Danach darf kein lokaler Check mehr
    schreiben (abo_finished). False, wenn die Wiederherstellung scheitert: dann keine
    Abschlussmeldung, der Aufrufer versucht es erneut."""
    if not rt.override.restore_and_clear(always_restore):
        return False
    rt.store.update(abo_finished=True)
    if final_notice:
        rt.notifier.notify("abo", "beendet", ABO_ENDED_MESSAGE, critical=True)
    else:
        logger.info("Abo-inaktiv-Frist ist bereits abgelaufen - Add-on bleibt ohne weitere Eingriffe im Ruhezustand.")
    return True


def report_restore(notifier, ok: bool) -> bool:
    """Eine dauerhaft scheiternde Wiederherstellung am Fristende wird einmal gemeldet
    (kritisch), die spaetere Rueckkehr ebenfalls. Gelingt sie gleich, bleibt es still. True bei einem
    Zustandswechsel: die Aufrufer loggen ERROR nur dann, nicht bei jedem zurueckgestellten Versuch."""
    if ok:
        return notifier.notify(RESTORE_KEY, STATE_OK, RESTORE_OK_MESSAGE, critical=True)
    return notifier.notify(RESTORE_KEY, RESTORE_FAILED_STATE, RESTORE_FAILED_MESSAGE, critical=True)


def log_restore_failure(rt: Runtime, message: str, *args) -> None:
    """Gescheiterte bzw. vom Schreibbudget zurueckgestellte Wiederherstellung am Fristende melden (report_restore) und
    loggen, hoechstens einmal je Episode sichtbar: ERROR beim Wechsel des Meldezustands, WARNING beim ersten
    Versuch dieses Prozesses, wenn der Zustand schon vor dem Neustart stand, danach im Takt nur DEBUG."""
    if report_restore(rt.notifier, ok=False):
        log = logger.error
    elif not rt.restore_failure_logged:
        log = logger.warning
    else:
        log = logger.debug
    rt.restore_failure_logged = True
    log(message, *args)


AUTH_REJECTED_QUERY_INTERVAL_SECONDS = 600.0
CONNECT_FAILURES_BEFORE_STATUS_QUERY = 3


def handle_connection_failing(rt: Runtime) -> None:
    """Bei IoT Core endet ein gesperrtes Zertifikat vermutlich im TLS-Aufbau statt mit
    CONNACK 134/135. Fehlt die Verbindung lange und scheitern die Versuche wiederholt
    (app._on_connection_check), liest das Add-on deshalb den Abo-Status aus dem Konfigurationsdokument,
    gedrosselt (AUTH_REJECTED_QUERY_INTERVAL_SECONDS). Nur ein eindeutiges Ergebnis wirkt: inactive ->
    Abo-inaktiv-Modus; active und unknown aendern nichts (normaler Ausfall, den Notbetrieb und Pruef-Tick
    abdecken)."""
    if rt.store.state.abo_inactive_since is not None:
        return
    now = rt.clock()
    last = rt.connection_failing_queried_at
    if last is not None and now - last < AUTH_REJECTED_QUERY_INTERVAL_SECONDS:
        return
    rt.connection_failing_queried_at = now
    status = entitlement.query(rt.config)
    if status == entitlement.INACTIVE:
        enter_inactive(rt, wallclock.now())
    else:
        logger.warning("MQTT-Verbindung fehlt seit Langem, Abo-Status ist '%s' - Verbindung pruefen.", status)


def check_grace_end(rt: Runtime) -> None:
    """Fristende waehrend der Laufzeit. Vorher erneut fragen (Abo-Status aus dem Konfigurationsdokument): ein
    inzwischen reaktiviertes Abo wird nicht zurueckgesetzt, sondern der Prozess startet im Normalbetrieb
    neu. Scheitert die
    Wiederherstellung, laeuft die lokale Regelung weiter und der naechste Check versucht es
    erneut."""
    since = rt.store.state.abo_inactive_since
    if since is None or not entitlement.grace_expired(since, wallclock.now()):
        return
    if entitlement.query(rt.config) == entitlement.ACTIVE:
        entitlement.clear(rt.config.entitlement_path)
        logger.warning("Abo wieder aktiv, Neustart im Normalbetrieb")
        rt.neustart()
        return
    if finish_grace(rt, always_restore=True, final_notice=True):
        report_restore(rt.notifier, ok=True)
        _enter_idle(rt)
        return
    log_restore_failure(
        rt,
        "Abo-inaktiv-Frist abgelaufen, zuletzt gelernte Werte konnten nicht wiederhergestellt "
        "werden - Notbetrieb laeuft weiter, erneuter Versuch beim naechsten Check",
    )


def _enter_idle(rt: Runtime) -> None:
    """Ruhezustand im Betrieb: MQTT und Trigger stoppen; ab jetzt laufen alle
    Handler ausser dem Lebenszeichen leer (app._unless_idle). Kein Exit."""
    rt.idle = True
    for client in (rt.mqtt_client, rt.trigger_client):
        if client is None:
            continue
        try:
            client.stop()
        except Exception:
            logger.exception("Verbindung konnte beim Wechsel in den Ruhezustand nicht sauber beendet werden")

