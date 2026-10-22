"""Ueberwachung der Aussentemperatur (R3-1), nach dem Muster von room_sensors. Liefert die zugeordnete Quelle
(Rolle outdoor_temp) dauerhaft keinen gueltigen Wert, bekommt der Kunde eine Meldung; die Rueckkehr entwarnt sofort.
Gueltig heisst wie im Telemetrie-Lesen: endliche Zahl im Bereich OUTDOOR_TEMP_RANGE.

Der Ausfall gilt erst nach room_sensors.FAILED_ROUNDS aufeinanderfolgenden Runden (Takt EV_HEALTH) ohne gueltigen
Wert (nach einem Host-Neustart laedt die Quelle noch, Stoerungen sind kurz). Der Zaehler (BridgeState.outdoor_misses) ist
reine Laufzeit; ein schon gemeldeter Ausfall bleibt nach einem Neustart bestehen, bis die Quelle wieder Werte liefert.
Ist keine Aussentemperatur zugeordnet, gibt es nichts zu pruefen. Nicht kritisch (nur Push); der Schluessel ist
Freitext (V1, device_checks des Servers), kein Vertrag."""
import logging

from smartheat_core.logtext import lesbar
from smartheat_runtime.notifier import STATE_OK
from smartheat_runtime.plausibility import OUTDOOR_TEMP_RANGE, is_plausible
from smartheat_runtime.ports import SignalNotFound, SourceUnavailable
from smartheat_runtime.room_sensors import FAILED_ROUNDS

logger = logging.getLogger(__name__)

KEY = "aussentemperatur"
STATE_FAILED = "ausgefallen"
FAILED_MESSAGE = "SmartHeat: Die Außentemperatur liefert keine gültigen Werte. Bitte die Quelle der Außentemperatur prüfen."
OK_MESSAGE = "SmartHeat: Die Außentemperatur liefert wieder Werte."


def check(rt) -> None:
    ref = rt.manifest.refs.get("outdoor_temp")
    if ref is None:
        return
    try:
        value = rt.signals.get_state(ref)
    except SignalNotFound:
        value = None
    except SourceUnavailable as error:
        logger.warning("Aussentemperatur-Pruefung abgebrochen, %s: %s", rt.texts.source_unavailable_log,
                       lesbar(str(error)))
        return
    except (ValueError, KeyError, TypeError):
        value = None
    if is_plausible(value, OUTDOOR_TEMP_RANGE):
        rt.store.update(outdoor_misses=0)
        rt.notifier.notify(KEY, STATE_OK, OK_MESSAGE, critical=False)
        return
    misses = min(rt.store.state.outdoor_misses + 1, FAILED_ROUNDS)
    rt.store.update(outdoor_misses=misses)
    if misses >= FAILED_ROUNDS:
        rt.notifier.notify(KEY, STATE_FAILED, FAILED_MESSAGE, critical=False)
