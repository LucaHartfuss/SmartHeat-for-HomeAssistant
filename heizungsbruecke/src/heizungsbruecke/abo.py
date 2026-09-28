"""Abo-inaktiv-Modus (Notbetrieb ohne Server bis zum Fristende) und Fristende."""
import logging
import os
import sys
from dataclasses import replace
from datetime import datetime

from heizungsbruecke import config, entitlement
from heizungsbruecke.notifier import STATE_OK
from heizungsbruecke.runtime import Runtime

logger = logging.getLogger(__name__)

ABO_ENDED_MESSAGE = (
    "SmartHeat: Abo seit 30 Tagen inaktiv. Die Heizungssteuerung ist beendet, "
    "die zuletzt gelernten Werte bleiben eingestellt."
)
ABO_ACTIVE_MESSAGE = "SmartHeat: Abo wieder aktiv, die Heizungssteuerung läuft wieder normal."
ACCESS_DENIED_MESSAGE = (
    "SmartHeat: Der Server lehnt die Zugangsdaten ab, die Heizkurve wird nicht mehr angepasst. "
    "Home Assistant fordert zur erneuten Anmeldung bei SmartHeat auf (Einstellungen → Geräte & Dienste)."
)
ACCESS_OK_MESSAGE = "SmartHeat: Die Zugangsdaten werden wieder angenommen, die Heizungssteuerung läuft wieder normal."
ACCESS_DENIED_REASON = "Zugangsdaten vom Server abgelehnt"
RESTORE_FAILED_MESSAGE = "SmartHeat: Zurücksetzen auf die zuletzt gelernten Werte scheitert. Bitte die Anlage prüfen."
RESTORE_OK_MESSAGE = "SmartHeat: Die zuletzt gelernten Werte sind wieder eingestellt."
RESTORE_FAILED_REASON = "Zurücksetzen auf die zuletzt gelernten Werte scheitert"
# Nur fuer den Log (silent_ok): kein Push, die Ablehnung ist mit dem Abo-inaktiv-Wechsel erledigt.
_ZUGANG_RESOLVED_BY_INACTIVE_MESSAGE = (
    "SmartHeat: Zugangsdaten-Hinweis durch den Wechsel in den Abo-inaktiv-Modus aufgehoben."
)


def inactive_message(inactive_since: datetime) -> str:
    end = entitlement.grace_end(inactive_since).strftime("%d.%m.%Y")
    return (
        f"SmartHeat: Abo inaktiv. Die Heizung läuft noch bis {end} im Notbetrieb weiter, "
        f"danach bleiben die zuletzt gelernten Werte fest eingestellt."
    )


def enter_inactive(rt: Runtime, now: datetime) -> None:
    """Notbetrieb an (persistiert), MQTT beendet, keine Snapshots/Telemetrie mehr. Gemeldet
    wird nur beim erstmaligen Setzen von inactive_since, nicht bei jedem Neustart.
    `mqtt_client.stop()` joint den paho-Thread; dessen Callbacks stellen nur ein und
    blockieren daher nie. Eine vorher gesetzte "zugang_abgelehnt" (eine Ablehnung bei noch
    unklarem Abo-Status vor dieser eindeutig inaktiven) wird aufgeloest: sonst haengt der
    falsche Rat ("neu anmelden") die ganze Kulanzfrist, und der Grund wuerde sogar noch im
    abo_beendet-Event am Fristende auftauchen (Fix Review Focus 1, Runde 1)."""
    state = rt.store.state
    if state.abo_inactive_since is not None:
        return
    try:
        since, newly_set = entitlement.mark_inactive(config.ENTITLEMENT_PATH, now)
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
    status = rt.status
    assert status is not None  # beim Boot gesetzt
    if status.flags.zugang_abgelehnt:
        status.update(zugang_abgelehnt=False, grund=None)
    rt.notifier.notify("zugang", STATE_OK, _ZUGANG_RESOLVED_BY_INACTIVE_MESSAGE, critical=True, silent_ok=True)
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


def report_restore(notifier, ok: bool) -> None:
    """T2-7: eine dauerhaft scheiternde Wiederherstellung am Fristende wird einmal gemeldet
    (kritisch), die spaetere Rueckkehr ebenfalls. Gelingt sie gleich, bleibt es still."""
    if ok:
        notifier.notify("wiederherstellung", STATE_OK, RESTORE_OK_MESSAGE, critical=True)
    else:
        notifier.notify("wiederherstellung", "fehlgeschlagen", RESTORE_FAILED_MESSAGE, critical=True)


def handle_auth_rejected(rt: Runtime) -> None:
    """Der Broker hat die Zugangsdaten abgelehnt (beim Suspend widerrufen). Nur ein eindeutiges
    "inactive" wechselt in den Abo-inaktiv-Modus, sonst meldet es "zugang_abgelehnt" und paho
    verbindet weiter. Die Abfrage blockiert den Worker bis zu 10 s; deshalb stellt der paho-Hook
    gebuendelt ein, und im Abo-inaktiv-Modus wird nicht mehr gefragt."""
    if rt.store.state.abo_inactive_since is not None:
        return
    status = entitlement.query_status(rt.options["tenant_id"], rt.options["accounts_api_base_url"])
    if status != entitlement.INACTIVE:
        logger.error(
            "MQTT-Anmeldung vom Broker abgelehnt, Abo-Status ist aber '%s' - Zugangsdaten "
            "pruefen (ggf. SmartHeat-Integration neu anmelden).", status,
        )
        status_reporter = rt.status
        assert status_reporter is not None  # beim Boot gesetzt
        status_reporter.update(zugang_abgelehnt=True, grund=ACCESS_DENIED_REASON)
        rt.notifier.notify("zugang", "abgelehnt", ACCESS_DENIED_MESSAGE, critical=True)
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
        restart_process()
        return
    if finish_grace(rt, always_restore=True, final_notice=True):
        report_restore(rt.notifier, ok=True)
        _enter_idle(rt)
        return
    report_restore(rt.notifier, ok=False)
    logger.error(
        "Abo-inaktiv-Frist abgelaufen, zuletzt gelernte Werte konnten nicht wiederhergestellt "
        "werden - Notbetrieb laeuft weiter, erneuter Versuch beim naechsten Check"
    )


def _enter_idle(rt: Runtime) -> None:
    """Ruhezustand im Betrieb (Spec TP7 3.2): MQTT und Trigger stoppen; ab jetzt laufen alle
    Handler ausser dem Lebenszeichen leer (__main__._unless_idle). Kein Exit."""
    rt.idle = True
    for client in (rt.mqtt_client, rt.trigger_client):
        if client is None:
            continue
        try:
            client.stop()
        except Exception:
            logger.exception("Verbindung konnte beim Wechsel in den Ruhezustand nicht sauber beendet werden")


def restart_process() -> None:
    """Ersetzt den Prozess durch einen frischen Start (Abo wieder aktiv, Neupruefung im
    Konfigurationsfehler). Unabhaengig vom Supervisor-Watchdog: der Container laeuft weiter."""
    os.execv(sys.executable, [sys.executable, "-m", "heizungsbruecke"])
