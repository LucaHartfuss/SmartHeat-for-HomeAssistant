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


class StatusSink(Protocol):
    def publish(self, event: dict) -> None:
        """Status-Modell (Schema 2) veroeffentlichen. Darf werfen; der Reporter sendet beim naechsten Anlass erneut."""
        ...


class NotifySink(Protocol):
    """Meldungen an den Kunden. Entprellung, Hinweis-Schalter und offene Meldungen fuehrt der Notifier."""

    def push(self, key: str, message: str) -> None:
        """Einmalige Nachricht (HA: alle Notify-Dienste). Fehler einzelner Kanaele behandelt der Sink selbst."""
        ...

    def show(self, key: str, message: str) -> None:
        """Offene kritische Meldung anlegen oder ersetzen (HA: persistent_notification). Darf werfen."""
        ...

    def withdraw(self, key: str) -> None:
        """Offene kritische Meldung zuruecknehmen. Darf werfen."""
        ...


class TriggerSource(Protocol):
    """Trigger-Eingang (Spec SHG 3.2): stellt Signal-Aenderungen und "Quelle verbunden" als Ereignis in den Worker
    (post_coalesced). HA: WebSocket-Trigger; SHG: lokaler Bus. `connected` steuert den Watchdog-Rueckfall."""

    @property
    def connected(self) -> bool: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...
