"""Einzelfuehler-Ueberwachung (Spec TP6 3.5). Faellt ein Raumfuehler aus, rechnet das
Raumtemperatur-Template mit den uebrigen weiter; der Kunde bekommt einen Hinweis. Mit nur einem
Fuehler ist dessen Ausfall der lokale Datenfehler auf room_actual (kritisch, delivery) und wird
hier nicht doppelt gemeldet. Gueltig heisst wie im Template: Zahl im Bereich ROOM_TEMP_RANGE."""
import logging

import requests

from heizungsbruecke.notifier import STATE_OK
from heizungsbruecke.plausibility import ROOM_TEMP_RANGE, is_plausible

logger = logging.getLogger(__name__)

STATE_FAILED = "ausgefallen"


def _message(ref: str, ok: bool, ok_count: int) -> str:
    entity_id = ref.partition("::")[0]
    if ok:
        return f"SmartHeat: Raumfühler {entity_id} liefert wieder Werte."
    if ok_count:
        return f"SmartHeat: Raumfühler {entity_id} liefert keine Werte, Mittelwert aus {ok_count} Fühlern."
    return (
        f"SmartHeat: Raumfühler {entity_id} liefert keine Werte. Kein Raumfühler liefert mehr "
        f"gültige Werte, die Heizkurve bleibt unverändert."
    )


def check_room_sensors(rt) -> None:
    sensors = rt.options["room_sensors"]
    if len(sensors) < 2:
        return
    valid = {}
    for ref in sensors:
        try:
            value = rt.ha_api.get_state(ref)
        except requests.HTTPError as error:
            if error.response is not None and error.response.status_code == 404:
                value = None
            else:
                logger.warning("Raumfuehler-Pruefung abgebrochen, Home Assistant nicht erreichbar: %s", error)
                return
        except requests.RequestException as error:
            logger.warning("Raumfuehler-Pruefung abgebrochen, Home Assistant nicht erreichbar: %s", error)
            return
        except (ValueError, KeyError, TypeError):
            value = None
        valid[ref] = is_plausible(value, ROOM_TEMP_RANGE)
    ok_count = sum(valid.values())
    for ref, ok in valid.items():
        rt.notifier.notify(
            f"raumfuehler:{ref.partition('::')[0]}", STATE_OK if ok else STATE_FAILED,
            _message(ref, ok, ok_count), critical=False,
        )
