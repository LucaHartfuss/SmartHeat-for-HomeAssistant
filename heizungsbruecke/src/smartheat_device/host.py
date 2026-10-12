"""Schnittstelle Host <-> Geraetekern (Spec 5b 5.1): was der Host dem Paket liefert (Soll-Quelle, Uhr, Inventur,
Befehle, Anzeige) und wie das Paket ihm Dokumente uebergibt. Gateway (Plan D2) und HA-Add-on (5c) setzen sie um. Alle
Rueckrufe laufen im Geraete-Thread, duerfen nicht lange blockieren (Befehle mit Wartezeit liefern Waiting) und keine
Ausnahme werfen: das Geraet faengt sie zwar ab und loggt nur Fehlerart und Ort, aber die Wirkung des Rueckrufs fehlt dann
(z. B. eine nicht angewandte Konfiguration bis zur naechsten Version, ein spaeteres hello)."""
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from smartheat_device.commands import CommandRegister


class MeldungSink:
    """NotifySink der Laufzeit (smartheat_runtime.ports) auf den Geraetekern (up/notifications), fuer jeden Host
    (Gateway allein, HA-Add-on neben HA-Benachrichtigungen). Regeln wie der Gateway-Agent bis 0.5.0: show = offene
    kritische Meldung, withdraw = geschlossen, push = Ereignis (offen und kritisch nur, solange der key offen ist).
    `melden(key, text, *, offen, kritisch)` ist Device.meldung (oder ein Host-Rueckruf darauf)."""

    def __init__(self, melden: Callable[..., None]) -> None:
        self._melden = melden
        self._offen: dict[str, str] = {}
        self._lock = threading.Lock()

    def push(self, key: str, message: str) -> None:
        with self._lock:
            offen = key in self._offen
        self._melden(key, message, offen=offen, kritisch=offen)

    def show(self, key: str, message: str) -> None:
        with self._lock:
            self._offen[key] = message
        self._melden(key, message, offen=True, kritisch=True)

    def withdraw(self, key: str) -> None:
        with self._lock:
            text = self._offen.pop(key, "")
        self._melden(key, text, offen=False, kritisch=True)


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

    @property
    def code_zeigen(self) -> bool:
        """Uebernahme-Code zeigen bzw. melden (Spec 5c 1.1, F1; Gateway-Seite und HA-Discovery gleich): nur solange
        der Server das Geraet als nicht uebernommen fuehrt und es weder startet noch gesperrt ist (der Sperrzustand
        steht nur im Speicher, device_state bleibt auf der Platte nicht_uebernommen)."""
        return (self.device_state == "nicht_uebernommen" and not self.eingerichtet
                and self.zustand not in ("startet", "gesperrt"))


class DeviceHost(Protocol):
    host: str  # wire.HOSTS
    version: str  # Software-Version (hello, register, Mindestversion)

    def faehigkeiten(self) -> dict:
        """hello.faehigkeiten bzw. register.capabilities: drivers, zigbee, optional updater, programm."""
        ...

    def uhr_synchron(self) -> bool | None:
        """Zeitdienst des Systems (Gateway: NTP); None = unbekannt, gilt als nicht synchron (Spec 3.3)."""
        ...

    def treiber_pruefen(self, bindung: dict, /) -> str | None:
        """Kundentext, wenn die Treiberparameter nicht passen (config_check, Regel 6), sonst None. Gateway: `bindung`; HA: die
        ganze Konfiguration (Spec 5c). Nur positional (config_check ruft so), daher darf der Host den Parameter
        passend benennen."""
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
