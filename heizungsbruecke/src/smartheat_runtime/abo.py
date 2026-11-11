"""Abo-inaktiv-Modus (Notbetrieb ohne Server bis zum Fristende) und Fristende."""
import logging
from dataclasses import replace
from datetime import datetime

from smartheat_core import wallclock
from smartheat_runtime import entitlement
from smartheat_runtime.notifier import STATE_OK
from smartheat_runtime.runtime import Runtime
from smartheat_runtime.soll_eingang import EV_SOLL_ENDE
from smartheat_runtime.state import StorageError
from smartheat_runtime.worker import Event

logger = logging.getLogger(__name__)

ABO_ENDED_MESSAGE = (
    "SmartHeat: Abo seit 30 Tagen inaktiv. Die Heizungssteuerung ist beendet, "
    "die ursprünglichen Werte sind wieder eingestellt."
)
ABO_ACTIVE_MESSAGE = "SmartHeat: Abo wieder aktiv, die Heizungssteuerung läuft wieder normal."
RESTORE_KEY = "wiederherstellung"
RESTORE_FAILED_STATE = "fehlgeschlagen"
RESTORE_FAILED_MESSAGE = "SmartHeat: Zurücksetzen auf die ursprünglichen Werte scheitert. Bitte die Anlage prüfen."
RESTORE_OK_MESSAGE = "SmartHeat: Die ursprünglichen Werte sind wieder eingestellt."
RESTORE_FAILED_REASON = "Zurücksetzen auf die ursprünglichen Werte scheitert"


def inactive_message(inactive_since: datetime) -> str:
    end = entitlement.grace_end(inactive_since).strftime("%d.%m.%Y")
    return (
        f"SmartHeat: Abo inaktiv. Die Heizung läuft noch bis {end} im Notbetrieb weiter, "
        f"danach stellt es die ursprünglichen Werte wieder ein und beendet die Steuerung."
    )


def enter_inactive(rt: Runtime, now: datetime) -> None:
    """Notbetrieb an (persistiert), Kanal des Geraetekerns gestoppt, keine Snapshots/Telemetrie mehr. Gemeldet
    wird nur beim erstmaligen Setzen von inactive_since, nicht bei jedem Neustart.
    `mqtt_client.stop()` schaltet den Kanal der Laufzeit ab (smartheat_device.laufzeit.RuntimeChannel nimmt keine
    Ereignisse mehr an); den Link betreibt weiter das Geraet."""
    state = rt.store.state
    if state.abo_inactive_since is not None:
        return
    if entitlement.load_inactive_since(rt.config.entitlement_path) is None and not rt.uhr_vertrauen():
        # Eine Frist auf unsicherer Uhr koennte zu frueh enden: sie beginnt erst, wenn die Uhr vertrauenswuerdig ist
        # (frist_nachholen). Bis dahin aendert sich nichts an der Anlage.
        rt.frist_offen = True
        logger.warning("Abo inaktiv, die Uhr ist nicht vertrauenswuerdig - die Frist beginnt, sobald sie es ist.")
        return
    rt.frist_offen = False
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
    Boosts beenden, die Werte von vor dem ersten Schreiben auf die Anlage. Danach darf kein lokaler Check mehr
    schreiben (abo_finished). False, wenn die Wiederherstellung scheitert: dann keine
    Abschlussmeldung, der Aufrufer versucht es erneut.

    Das erste Ende wird dauerhaft gemerkt (entitlement.mark_beendet) und setzt den Merker "Soll-Ende offen": die
    Soll-Quelle soll den gemerkten Wunsch zeigen, der Merker faellt erst nach Erfolg (soll_eingang.EV_SOLL_ENDE_ERLEDIGT).
    Ein weiterer Aufruf nach dem Ende tut nichts mehr. Reihenfolge: erst der Zustand, dann die Datei; stirbt der Prozess
    dazwischen, wiederholt sich nur das erste Ende. Scheitert das Speichern der Datei, wiederholt der Takt des laufenden
    Prozesses es (`ende_festhalten`: EV_HEALTH im Betrieb, EV_RECHECK im Ruhezustand)."""
    if rt.store.state.abo_finished:
        return True
    if not rt.override.restore_and_clear(always_restore):
        return False
    erstes = not entitlement.ist_beendet(rt.config.entitlement_path)
    try:
        # Schreibfehler: der Zustand steht schon im Speicher; im laufenden Prozess holt der Takt EV_HEALTH das Schreiben
        # nach, im Ruhezustand (die IdleBridge hat kein EV_HEALTH) gilt er bis zum Neustart
        if erstes:
            rt.store.update(abo_finished=True, soll_ende_offen=True)
        else:
            rt.store.update(abo_finished=True)
    except StorageError:
        logger.exception("Das Ende der Regelung konnte in backup.json nicht gespeichert werden")
    if erstes:
        ende_festhalten(rt)
    rt.worker.post(Event(EV_SOLL_ENDE))
    if final_notice:
        rt.notifier.notify("abo", "beendet", ABO_ENDED_MESSAGE, critical=True)
    else:
        logger.info("Abo-inaktiv-Frist ist bereits abgelaufen - Add-on bleibt ohne weitere Eingriffe im Ruhezustand.")
    return True


def ende_festhalten(rt: Runtime) -> bool:
    """Speichert das Ende dauerhaft (entitlement.mark_beendet), solange es noch nicht steht. True = gespeichert. Bei
    einem Fehler False und eine Warnung: der Aufrufer versucht es im naechsten Takt erneut, bis es gelingt."""
    if entitlement.ist_beendet(rt.config.entitlement_path):
        return True
    try:
        entitlement.mark_beendet(rt.config.entitlement_path)
    except Exception as error:
        logger.warning("Das Ende der Regelung konnte nicht gespeichert werden (%s) - erneuter Versuch im naechsten Takt",
                       type(error).__name__)
        return False
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


def frist_nachholen(rt: Runtime) -> None:
    """Der Beginn einer Abo-Frist, den `enter_inactive` wegen unsicherer Uhr zurueckgestellt hat, im Takt des lokalen
    Checks: sobald die Uhr vertrauenswuerdig ist und der Abo-Status weiter inaktiv ist, beginnt die Frist jetzt. Wurde das
    Abo inzwischen aktiv, entfaellt der offene Beginn."""
    if not rt.frist_offen:
        return
    if entitlement.query(rt.config) != entitlement.INACTIVE:
        rt.frist_offen = False
        return
    if rt.uhr_vertrauen():
        enter_inactive(rt, wallclock.now())


def check_grace_end(rt: Runtime) -> None:
    """Fristende waehrend der Laufzeit. Vorher erneut fragen (Abo-Status aus dem Konfigurationsdokument): ein
    inzwischen reaktiviertes Abo wird nicht zurueckgesetzt, sondern der Prozess startet im Normalbetrieb
    neu. Scheitert die
    Wiederherstellung, laeuft die lokale Regelung weiter und der naechste Check versucht es
    erneut."""
    since = rt.store.state.abo_inactive_since
    if rt.store.state.abo_finished or entitlement.ist_beendet(rt.config.entitlement_path):
        return  # das Ende ist schon abgewickelt (Regelung beendet), nichts mehr zu tun
    if since is None or not rt.uhr_vertrauen() or not entitlement.grace_expired(since, wallclock.now()):
        return  # eine unsichere Uhr beendet nie: die Frist verlaengert sich hoechstens
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
        "Abo-inaktiv-Frist abgelaufen, ursprüngliche Werte konnten nicht wiederhergestellt "
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

