"""Abo-inaktiv-Modus (Notbetrieb ohne Server bis zum Fristende) und Fristende."""
import logging
import os
import sys
from dataclasses import replace
from datetime import datetime

from heizungsbruecke import config, entitlement
from heizungsbruecke.runtime import Runtime

logger = logging.getLogger(__name__)

# Stabile notification_id: wiederholte Meldungen ersetzen sich in HA statt sich zu stapeln.
ABO_NOTIFICATION_ID = "smartheat_abo_inaktiv"
ABO_NOTIFICATION_TITLE = "SmartHeat"
ABO_ENDED_MESSAGE = (
    "SmartHeat: Abo seit 30 Tagen inaktiv. Die Heizungssteuerung ist beendet, "
    "die zuletzt gelernten Werte bleiben eingestellt."
)


def inactive_message(inactive_since: datetime) -> str:
    end = entitlement.grace_end(inactive_since).strftime("%d.%m.%Y")
    return (
        f"SmartHeat: Abo inaktiv. Die Heizung läuft noch bis {end} im Notbetrieb weiter, "
        f"danach bleiben die zuletzt gelernten Werte fest eingestellt."
    )


def notify(ha_api, notify_service: str, message: str) -> None:
    """Log, Push (falls konfiguriert) und HA-persistent_notification; jeder Kanal best effort."""
    logger.warning(message)
    if notify_service:
        try:
            ha_api.send_notification(notify_service, message)
        except Exception:
            logger.warning("Push-Benachrichtigung zum Abo-Status konnte nicht gesendet werden")
    try:
        ha_api.create_persistent_notification(ABO_NOTIFICATION_TITLE, message, ABO_NOTIFICATION_ID)
    except Exception:
        logger.warning("HA-Benachrichtigung zum Abo-Status konnte nicht angelegt werden")


def enter_inactive(rt: Runtime, now: datetime) -> None:
    """Notbetrieb an (persistiert), MQTT beendet, keine Snapshots/Telemetrie mehr. Gemeldet
    wird nur beim erstmaligen Setzen von inactive_since, nicht bei jedem Neustart.
    `mqtt_client.stop()` joint den paho-Thread; dessen Callbacks stellen nur ein und
    blockieren daher nie."""
    state = rt.store.state
    if state.abo_inactive_since is not None:
        return
    try:
        since, newly_set = entitlement.mark_inactive(config.ENTITLEMENT_PATH, now)
    except Exception:
        # Ein Schreibfehler (SD-Karte) darf den Notbetrieb nicht verhindern.
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
        notify(rt.ha_api, rt.options.get("notify_service", ""), inactive_message(since))
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
        notify(rt.ha_api, rt.options.get("notify_service", ""), ABO_ENDED_MESSAGE)
    else:
        logger.info("Abo-inaktiv-Frist ist bereits abgelaufen - Add-on beendet sich ohne weitere Eingriffe.")
    return True


def handle_auth_rejected(rt: Runtime) -> None:
    """Der Broker hat die Zugangsdaten abgelehnt (beim Suspend widerrufen). Nur ein eindeutiges
    "inactive" wechselt in den Abo-inaktiv-Modus, sonst nur ein Log und paho verbindet weiter.
    Die Abfrage blockiert den Worker bis zu 10 s; deshalb stellt der paho-Hook gebuendelt ein,
    und im Abo-inaktiv-Modus wird nicht mehr gefragt."""
    if rt.store.state.abo_inactive_since is not None:
        return
    status = entitlement.query_status(rt.options["tenant_id"], rt.options["accounts_api_base_url"])
    if status != entitlement.INACTIVE:
        logger.error(
            "MQTT-Anmeldung vom Broker abgelehnt, Abo-Status ist aber '%s' - Zugangsdaten "
            "pruefen (ggf. SmartHeat-Integration neu einrichten).", status,
        )
        return
    enter_inactive(rt, datetime.now().astimezone())


def check_grace_end(rt: Runtime) -> None:
    """Fristende waehrend der Laufzeit. Vorher erneut fragen: ein inzwischen reaktiviertes Abo
    wird nicht zurueckgesetzt, sondern der Prozess startet im Normalbetrieb neu (set-status
    active stellt widerrufene MQTT-Zugangsdaten nicht wieder her, dafuer muss die Integration
    neu eingerichtet werden). Scheitert die Wiederherstellung, laeuft die lokale Regelung
    weiter und der naechste Check versucht es erneut."""
    since = rt.store.state.abo_inactive_since
    if since is None or not entitlement.grace_expired(since, datetime.now().astimezone()):
        return
    if entitlement.query_status(rt.options["tenant_id"], rt.options["accounts_api_base_url"]) == entitlement.ACTIVE:
        entitlement.clear(config.ENTITLEMENT_PATH)
        logger.warning("Abo wieder aktiv, Neustart im Normalbetrieb")
        _restart_process()
        return
    if finish_grace(rt, always_restore=True, final_notice=True):
        if rt.trigger_client is not None:
            rt.trigger_client.stop()
        rt.worker.request_exit(0)
        return
    logger.error(
        "Abo-inaktiv-Frist abgelaufen, zuletzt gelernte Werte konnten nicht wiederhergestellt "
        "werden - Notbetrieb laeuft weiter, erneuter Versuch beim naechsten Check"
    )


def _restart_process() -> None:
    """Ersetzt den Prozess durch einen frischen Start im Normalbetrieb, unabhaengig vom
    Supervisor-Watchdog (config.yaml setzt bewusst keinen)."""
    os.execv(sys.executable, [sys.executable, "-m", "heizungsbruecke"])
