"""Anbindung der Waermelieferungs-Erkennung (waerme.py) an Zustand, Meldung und Status (TP12f, Spec 2)."""
import logging
from datetime import datetime

from heizungsbruecke.notifier import STATE_OK
from smartheat_core import wallclock
from smartheat_runtime import waerme
from smartheat_runtime.state import StorageError

logger = logging.getLogger(__name__)

NOTIFY_KEY = "therme"
STATE_FEHLT = "fehlt"
MESSAGE_CLEARED = "SmartHeat: Die Therme liefert wieder Heizwärme, die Heizkurvenoptimierung läuft weiter."


def _message_set(since: datetime) -> str:
    return (
        "SmartHeat: Die Therme liefert keine Heizwärme, obwohl die Heizung Wärme anfordert "
        f"(seit {since:%H:%M}). Bitte an der Therme prüfen, ob der Heizbetrieb eingeschaltet ist "
        "(Sommerbetrieb, Statuscode S.031). Die Heizkurvenoptimierung pausiert so lange."
    )


def apply_tick(rt, room_actual, kpi_fields: dict, regulation_fields: dict, now: datetime | None = None) -> bool:
    """Bewertet einen Telemetrie-Tick, speichert das Flag und meldet einen Wechsel (Kundenhinweis
    `therme`, Status-Event). Gibt zurueck, ob das Flag danach gesetzt ist. Wirft nie: ein Fehler in
    Speicher, Meldung oder Status darf die Telemetrie nicht verhindern."""
    state = rt.store.state
    previous = state.waerme or waerme.WaermeState(fehlt_seit=waerme.parse_since(state.waerme_fehlt_seit))
    try:
        current = waerme.evaluate(
            previous, now or wallclock.now(),
            regulation_fields.get("flow_setpoint"), kpi_fields.get("flow_temperature"), room_actual,
        )
    except Exception:
        logger.exception("Waermelieferung konnte nicht bewertet werden, der bisherige Zustand gilt")
        return previous.fehlt_seit is not None
    since_text = None if current.fehlt_seit is None else current.fehlt_seit.isoformat()
    try:
        rt.store.update(waerme=current, waerme_fehlt_seit=since_text)
    except StorageError:
        logger.warning("waerme_fehlt_seit konnte nicht gespeichert werden, der Zustand gilt im Speicher weiter")
    except Exception:
        logger.exception("waerme_fehlt_seit konnte nicht gespeichert werden")
    if (previous.fehlt_seit is None) != (current.fehlt_seit is None):
        try:
            if current.fehlt_seit is not None:
                rt.notifier.notify(NOTIFY_KEY, STATE_FEHLT, _message_set(current.fehlt_seit), critical=False)
            else:
                rt.notifier.notify(NOTIFY_KEY, STATE_OK, MESSAGE_CLEARED, critical=False, silent_ok=True)
            if rt.status is not None:
                rt.status.publish_if_changed()
        except Exception:
            logger.exception("Meldung zur Waermelieferung konnte nicht gesendet werden")
    return current.fehlt_seit is not None
