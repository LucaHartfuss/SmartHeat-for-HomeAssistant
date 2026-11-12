"""Stand der Integration aus ihrem Lebenszeichen smartheat_integration {schema, version, v5_schemas}.
Das Ereignis kommt vom HA-Event-Bus und ist nicht vertrauenswuerdig: gelesen werden nur der eingefrorene Kern (schema,
version, v5_schemas) in strenger Form, unbekannte Felder werden ignoriert, ein unlesbares Ereignis aendert nichts und
verlaengert nichts. Eine Faelschung kann hoechstens eine Version oder den Hinweis „Integration aelter als das
Status-Schema des Add-ons“ zeigen. Zeit kommt als monotone Sekunden vom Aufrufer, nie von der Wanduhr. Versionen und Schemata
gehoeren nicht in Logs."""
import re
import threading

from heizungsbruecke import geraet_status

#: Ohne Ereignis seit so vielen Sekunden ist die Version der Integration unbekannt (die Integration sendet alle 300 s).
FRIST_SECONDS = 900
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,31}")
_MAX_SCHEMATA = 16


def _ganzzahl(wert, *, ab: int = 1) -> bool:
    return isinstance(wert, int) and not isinstance(wert, bool) and wert >= ab


def _lesen(daten) -> tuple[str, bool] | None:
    """(Version, kann das gesendete Status-Schema nicht lesen und ist aelter) aus einem Ereignis; None, wenn der Kern nicht lesbar ist."""
    if not isinstance(daten, dict) or not _ganzzahl(daten.get("schema")):
        return None
    version, schemata = daten.get("version"), daten.get("v5_schemas")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        return None
    if not isinstance(schemata, list) or not 1 <= len(schemata) <= _MAX_SCHEMATA \
            or not all(_ganzzahl(s) for s in schemata):
        return None
    return version, geraet_status.EVENT_SCHEMA not in schemata and max(schemata) < geraet_status.EVENT_SCHEMA


class IntegrationStand:
    """Letzte lesbare Aussage der Integration. Der WS-Thread ruft ereignis(), andere Threads fragen ab."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._aussage: tuple[str, bool] | None = None
        self._zeit = 0.0

    def _gueltig(self, jetzt: float) -> tuple[str, bool] | None:
        if self._aussage is None or jetzt - self._zeit >= FRIST_SECONDS:
            return None
        return self._aussage

    def ereignis(self, daten, jetzt: float) -> bool:
        """Nimmt ein Ereignis auf; True, wenn sich Version oder Hinweis gegenueber dem Stand davor geaendert haben
        (auch nach Ablauf der Frist), sonst False. Ein unlesbares Ereignis aendert nichts."""
        neu = _lesen(daten)
        if neu is None:
            return False
        with self._lock:
            alt = self._gueltig(jetzt)
            self._aussage, self._zeit = neu, jetzt
            return alt != neu

    def version(self, jetzt: float) -> str | None:
        """Version der Integration; None ohne lesbares Ereignis oder nach FRIST_SECONDS ohne eines."""
        with self._lock:
            aussage = self._gueltig(jetzt)
        return aussage[0] if aussage else None

    def veraltet(self, jetzt: float) -> bool:
        """Die Integration liest das Status-Schema nicht, das das Add-on sendet, und nur aeltere (ist sie neuer, zeigt sie das selbst)."""
        with self._lock:
            aussage = self._gueltig(jetzt)
        return bool(aussage and aussage[1])
