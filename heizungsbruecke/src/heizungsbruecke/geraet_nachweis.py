"""Nachweis-Ereignis des HA-Add-ons: schreibt die Integration beim Beginn der Einrichtung `nachweis <geraet_id>` ans
stdin (stdin_befehl), signiert der Geraetekern mit dem Geraeteschluessel (identity.nachweis) und sendet das Ereignis
smartheat_nachweis {schema, geraet_id, zeitstempel, signatur} auf den HA-Event-Bus. Die Integration reicht Signatur und
Zeit an den Server weiter, der damit pruefen kann, dass das Add-on das Geraet ist. Das Ereignis ist einmalig und traegt
kein Geheimnis (nie den Schluessel, nie den Uebernahme-Code); ein Fehler endet in einer WARNING mit der Fehlerart, nie mit
Signatur oder Antworttext. Felder sind Cross-Repo-Vertrag mit der Integration (Contract-Check)."""
import logging
import time
from collections.abc import Callable

from smartheat_device import identity

logger = logging.getLogger(__name__)

EVENT_TYPE = "smartheat_nachweis"
EVENT_SCHEMA = 1
EVENT_FIELDS = ("schema", "geraet_id", "zeitstempel", "signatur")


def senden(ha_api, ident: identity.Identity, uhr: Callable[[], float] = time.time) -> None:
    """Nachweis jetzt (ganze Unix-Sekunden aus uhr) signieren und als Ereignis senden. Wirft nicht: der Aufrufer ist
    der Zeilenleser des stdin, der weiterlesen muss; bleibt das Ereignis aus, zeigt die Integration den Fehlerweg."""
    try:
        ereignis = {"schema": EVENT_SCHEMA, **identity.nachweis(ident, int(uhr()))}
        ha_api.fire_event(EVENT_TYPE, ereignis)
    except Exception as error:
        logger.warning("Nachweis nicht gesendet (%s)", type(error).__name__)
