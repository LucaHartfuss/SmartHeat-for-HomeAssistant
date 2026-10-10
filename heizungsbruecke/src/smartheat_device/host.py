"""Schnittstelle Host <-> Geraetekern (Spec 5b 5.1): was der Host dem Paket liefert (Soll-Quelle, Uhr, Inventur,
Befehle, Anzeige) und wie das Paket ihm Dokumente uebergibt. Gateway (Plan D2) und HA-Add-on (5c) setzen sie um. Alle
Rueckrufe laufen im Geraete-Thread und duerfen nicht lange blockieren (Befehle mit Wartezeit liefern Waiting)."""
from dataclasses import dataclass
from typing import Protocol

from smartheat_device.commands import CommandRegister


@dataclass(frozen=True)
class Anzeige:
    """Was der Host anzeigt (Gateway: LED und Diagnoseseite; HA: Status und Reparatur-Hinweise, 5c)."""
    zustand: str  # wire.ZUSTAENDE
    server_ok: bool  # Link verbunden
    device_state: str | None  # zuletzt bekannter Uebernahmestatus (Bootstrap)
    eingerichtet: bool
    hinweise: tuple[dict, ...]  # Befehl hinweis: {key, stufe, text}
    meldungen: tuple[dict, ...]  # eigene Meldungen des Geraets (wunsch_begrenzt, soll_quelle_aus): {key, text}
    konfiguration_abgelehnt: str | None  # Text der zuletzt abgelehnten Konfiguration


class DeviceHost(Protocol):
    host: str  # wire.HOSTS
    version: str  # Software-Version (hello, register, Mindestversion)

    def faehigkeiten(self) -> dict:
        """hello.faehigkeiten bzw. register.capabilities: drivers, zigbee, optional updater, programm."""
        ...

    def uhr_synchron(self) -> bool | None:
        """Zeitdienst des Systems (Gateway: NTP); None = unbekannt, gilt als nicht synchron (Spec 3.3)."""
        ...

    def treiber_pruefen(self, bindung: dict) -> str | None:
        """Kundentext, wenn die Treiberparameter nicht passen (config_check, Regel 6), sonst None."""
        ...

    def befehle_eintragen(self, register: CommandRegister) -> None: ...

    def konfiguration_uebernommen(self, alt: dict | None, neu: dict) -> None:
        """Neue Konfiguration (auch leer = nicht eingerichtet, A4-07). Gateway: Laufzeit neu starten, wenn
        dokument_config.laufzeit_relevant."""
        ...

    def bedienung_uebernommen(self, inhalt: dict) -> None:
        """Neue Bedienung: raum_soll anwenden und die Soll-Quelle angleichen, wenn sie schreibbar ist (Spec 3.3)."""
        ...

    def software_soll(self, software: dict | None) -> None:
        """Feld software jeder Konfiguration mit bekanntem Schema, auch einer abgelehnten (Updater, Spec 5.3)."""
        ...

    def runtime_status(self) -> dict | None: ...

    def raum(self) -> dict | None: ...

    def anzeigen(self, anzeige: Anzeige) -> None:
        """Jeden Takt; der Host drosselt selbst (LED-Datei, Seite)."""
        ...
