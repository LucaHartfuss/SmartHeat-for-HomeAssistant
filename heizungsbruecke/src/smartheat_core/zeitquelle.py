"""Zeitquelle des Gerätekerns: fachliche Wanduhr und Epoche (folgen einem Zeitraffer) und echte Epoche (nie verschoben).

Nur in einem Testlauf (`SMARTHEAT_TESTKENNUNG` nicht leer) darf die fachliche Zeit um einen festen Versatz nach vorn
geschoben werden: `SMARTHEAT_ZEITRAFFER_VERSATZ_S`, eine Dezimalzahl ohne Vorzeichen, Exponent und Unterstriche, von 0 bis
zwei Jahre (nie negativ: der Server lehnt Zeitstempel aus der Zukunft ab, der Versatz wirkt nur vorwärts). Es gibt
keinen Faktor und keine Epoche: Wartefristen, MQTT und Threads laufen in echter Zeit, ein Faktor ließe die monotone Uhr
gegen die Hardware driften. Die Grammatik ist die der Uhr des Servers (`gemeinsam/uhr.py`).

Die Umgebung wird beim Import einmal gelesen. Jede gesetzte Variable mit dem Präfix `SMARTHEAT_ZEITRAFFER_` ohne nicht
leere Testkennung (eine Kennung nur aus Leerzeichen zählt als nicht gesetzt), jede andere Variable des Präfixes als der
Versatz und jeder leere oder ungültige Wert verhindern den Start mit einer festen Meldung ohne Namen und Werte. Ist ein
Zeitraffer gültig, schreibt der Import einmal die Warnung "Zeitraffer aktiv" (ohne Werte). Weil `wallclock` dieses Modul
importiert, trägt jeder Prozess, der den Kern lädt, diese Sperre. Die Testkennung schützt gegen Versehen, nicht gegen
Absicht. Das Vertrauen in die Uhr (`wallclock.vertrauenswuerdig`) bleibt unberührt: der Versatz ersetzt es nicht.
Nur Standardbibliothek."""
import logging
import os
import re
import time
from datetime import UTC, datetime

_TESTKENNUNG = "SMARTHEAT_TESTKENNUNG"
_PRAEFIX = "SMARTHEAT_ZEITRAFFER_"
_VERSATZ = "SMARTHEAT_ZEITRAFFER_VERSATZ_S"
_VERSATZ_MAX_S = 63_072_000.0  # zwei Jahre
_ZAHL = re.compile(r"[0-9]+(\.[0-9]+)?")
_MELDUNG = "Zeitraffer ohne Testkennung oder ungültig: Start verweigert"


def echte_epoche() -> float:
    """Echte Zeit in Sekunden seit der Epoche, nie verschoben (Signaturen, Hersteller-API, Hoststatus)."""
    return time.time()


def _lesen() -> float:
    """Versatz in Sekunden aus der Umgebung (0 ohne Zeitraffer); wirft `RuntimeError` mit festem Text ohne Namen und Wert."""
    gesetzt = [name for name in os.environ if name.startswith(_PRAEFIX)]
    if not gesetzt:
        return 0.0
    try:
        if not os.environ.get(_TESTKENNUNG, "").strip() or any(name != _VERSATZ for name in gesetzt):
            raise ValueError
        roh = os.environ[_VERSATZ]
        if not _ZAHL.fullmatch(roh):
            raise ValueError
        versatz = float(roh)
        if not 0.0 <= versatz <= _VERSATZ_MAX_S:
            raise ValueError
        datetime.fromtimestamp(echte_epoche() + versatz, UTC)  # nicht darstellbar scheitert hier, nicht erst im Betrieb
    except (ValueError, OverflowError, OSError):
        raise RuntimeError(_MELDUNG) from None
    return versatz


_VERSATZ_S = _lesen()
if any(name.startswith(_PRAEFIX) for name in os.environ):
    logging.getLogger(__name__).warning("Zeitraffer aktiv")


def fachliche_epoche() -> float:
    """Fachliche Zeit in Sekunden seit der Epoche: die echte Zeit plus Versatz."""
    return echte_epoche() + _VERSATZ_S


def fachliche_wanduhr() -> datetime:
    """Fachliche Zeit als Ortszeit mit Zeitzone."""
    return datetime.fromtimestamp(fachliche_epoche(), UTC).astimezone()
