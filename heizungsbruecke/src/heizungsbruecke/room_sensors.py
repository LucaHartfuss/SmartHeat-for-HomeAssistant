"""Einzelfuehler-Ueberwachung (Spec TP6 3.5). Faellt ein Raumfuehler aus, rechnet das
Raumtemperatur-Template mit den uebrigen weiter; der Kunde bekommt einen Hinweis. Mit nur einem
Fuehler ist dessen Ausfall der lokale Datenfehler auf room_actual (kritisch, delivery) und wird
hier nicht doppelt gemeldet. Gueltig heisst wie im Template: Zahl im Bereich ROOM_TEMP_RANGE.

Ein Fuehler gilt erst nach FAILED_ROUNDS aufeinanderfolgenden Runden ohne gueltigen Wert als
ausgefallen (nach einem Host-Neustart laedt z.B. Zigbee noch, Funkstoerungen sind kurz); die
Rueckkehr wird sofort gemeldet. Der Zaehler ist reine Laufzeit und beginnt nach einem Neustart neu;
ein schon gemeldeter Ausfall bleibt dabei bestehen, bis der Fuehler wieder Werte liefert."""
import logging

from heizungsbruecke.notifier import STATE_OK
from smartheat_runtime.plausibility import ROOM_TEMP_RANGE, is_plausible
from smartheat_runtime.ports import SignalNotFound, SourceUnavailable

logger = logging.getLogger(__name__)

STATE_FAILED = "ausgefallen"
FAILED_ROUNDS = 2


def _message(ref: str, ok: bool, ok_count: int) -> str:
    entity_id = ref.partition("::")[0]
    if ok:
        return f"SmartHeat: Raumfühler {entity_id} liefert wieder Werte."
    if ok_count:
        sensors = "Fühler" if ok_count == 1 else "Fühlern"
        return f"SmartHeat: Raumfühler {entity_id} liefert keine Werte, Mittelwert aus {ok_count} {sensors}."
    return (
        f"SmartHeat: Raumfühler {entity_id} liefert keine Werte. Kein Raumfühler liefert mehr "
        f"gültige Werte, die Heizkurve bleibt unverändert."
    )


def check_room_sensors(rt) -> None:
    sensors = rt.config.room_sensor_refs
    if len(sensors) < 2:
        return
    valid = {}
    for ref in sensors:
        try:
            value = rt.signals.get_state(ref)
        except SignalNotFound:
            value = None
        except SourceUnavailable as error:
            logger.warning("Raumfuehler-Pruefung abgebrochen, Home Assistant nicht erreichbar: %s", error)
            return
        except (ValueError, KeyError, TypeError):
            value = None
        valid[ref] = is_plausible(value, ROOM_TEMP_RANGE)
    ok_count = sum(valid.values())
    previous_misses = rt.store.state.room_sensor_misses
    misses = {}
    for ref, ok in valid.items():
        if not ok:
            misses[ref] = min(previous_misses.get(ref, 0) + 1, FAILED_ROUNDS)
            if misses[ref] < FAILED_ROUNDS:
                continue
        rt.notifier.notify(
            f"raumfuehler:{ref.partition('::')[0]}", STATE_OK if ok else STATE_FAILED,
            _message(ref, ok, ok_count), critical=False,
        )
    rt.store.update(room_sensor_misses=misses)
