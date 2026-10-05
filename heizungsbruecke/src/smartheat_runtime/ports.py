"""Ports des hostneutralen Kerns (Spec SHG 3.2): die einzigen Stellen, an denen die Laufzeit die Aussenwelt beruehrt.
Die Anlage ist smartheat_core.binding.PlantBinding, die Wanduhr smartheat_core.wallclock; die monotone Uhr wird als
Callable[[], float] uebergeben."""
from typing import Protocol


class SignalNotFound(LookupError):
    """Die Referenz gibt es bei der Quelle nicht (HA: HTTP 404). Ueberwachungen ueberspringen sie."""


class SourceUnavailable(OSError):
    """Die Quelle antwortet nicht (HA nicht erreichbar, Zeitueberschreitung, andere HTTP-Fehler). Ueberwachungen beenden
    die Runde ohne Meldung."""


class SignalSource(Protocol):
    """Werte ueber hostspezifische Referenzen lesen (HA: Entity-ID bzw. `entity::attribut`). Ein ungueltiger Wert
    (nicht verfuegbar, keine Zahl, Attribut fehlt) wirft ValueError, KeyError oder TypeError."""

    def get_state(self, ref: str) -> float: ...

    def get_raw_state(self, ref: str) -> str: ...
