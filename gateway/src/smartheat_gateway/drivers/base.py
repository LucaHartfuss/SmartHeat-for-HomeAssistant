"""Treiber-Protokoll (Spec SHG G2 4.1): ein Treiber IST ein PlantBinding aus smartheat_core mit derselben
BindingDescription wie der HA-Pfad, plus Signale, Abfrage-Thread, Login, Erkennung und Inventur. Lesen kommt aus dem Cache;
aelter als CACHE_MAX_POLLS x poll_seconds (monotone Uhr) -> ValueError, die Datenfehler-Logik der Laufzeit greift."""
from collections.abc import Mapping
from typing import ClassVar, Protocol

from smartheat_core.binding import PlantBinding
from smartheat_device import wire
from smartheat_device.nachrichten import pruefen
from smartheat_runtime.roles import LEVER_ROLES

__all__ = [
    "CACHE_MAX_POLLS", "KIND_CLOUD", "KIND_LOCAL", "LEVER_ROLES", "LOGIN_OAUTH", "LOGIN_PASSWORD", "Driver",
    "DRIVER_ERROR_TEXTE", "DriverError", "erkennung_daten",
]

KIND_CLOUD = "cloud"
KIND_LOCAL = "local"
LOGIN_OAUTH = "oauth_pkce"
LOGIN_PASSWORD = "passwort_verschluesselt"
CACHE_MAX_POLLS = 3


#: Lokaler Text je Vertragsgrund fuer die Meldung einer DriverError (Log, Meldungen des Geraets, z. B. ein Schreibfehler
#: bei abgelaufener Anmeldung). Er geht nie an den Server: dorthin gehen nur grund und details (Spec Stufe 3 S5 6.2/6.3).
DRIVER_ERROR_TEXTE = {
    "nicht_angemeldet": "Die Anmeldung beim Hersteller ist abgelaufen. Bitte im SmartHeat-Portal die Anmeldung erneuern.",
    "anlage_nicht_erreichbar": "Die Anlage oder die Hersteller-Cloud ist nicht erreichbar.",
    "login_fehlgeschlagen": "Die Anmeldung beim Hersteller ist fehlgeschlagen. Bitte noch einmal anmelden.",
    "login_nicht_noetig": "Für diesen Zugang ist keine Anmeldung nötig.",
    "treiber_unbekannt": "Dieser Hersteller-Zugang wird vom Gateway nicht unterstützt.",
}
DRIVER_ERROR_TEXT_FALLBACK = "Der Hersteller-Zugang meldet einen Fehler."


def erkennung_daten(treiber: str, anlagen: list[dict], features: list[dict]) -> dict:
    """Ergebnis von Driver.erkennung() mit genau den Feldern des Vertrags (wire.INVENTUR_GATEWAY_FIELDS, je Anlage
    INVENTUR_ANLAGE_FIELDS, je Feature INVENTUR_FEATURE_FIELDS). Jeder Treiber baut seine Erkennung hier; eine Abweichung
    ist ein Vertragsfehler mit Feldnamen, nie mit Werten aus der Cloud (Regel 6): der Befehl endet mit intern."""
    for anlage in anlagen:
        pruefen("inventur.anlage", anlage, wire.INVENTUR_ANLAGE_FIELDS)
    for feature in features:
        pruefen("inventur.feature", feature, wire.INVENTUR_FEATURE_FIELDS)
    return pruefen("inventur", {"treiber": treiber, "anlagen": anlagen, "features": features},
                   wire.INVENTUR_GATEWAY_FIELDS)


class DriverError(RuntimeError):
    """Fehler mit Vertragsgrund (smartheat_device/wire.py; gemeldet vom Geraetekern ueber die Befehle driver_login und
    inventory): nicht_angemeldet, anlage_nicht_erreichbar, login_fehlgeschlagen, login_nicht_noetig,
    treiber_unbekannt. Zum Server gehen nur grund und details (den Text bildet der Server, Spec Stufe 3 S5 6.2); die
    Meldung der Ausnahme ist ein lokaler Text (DRIVER_ERROR_TEXTE) fuer Log und lokale Meldungen (6.3)."""

    def __init__(self, grund: str, details: dict | None = None) -> None:
        super().__init__(DRIVER_ERROR_TEXTE.get(grund, DRIVER_ERROR_TEXT_FALLBACK))
        self.grund = grund
        self.details = details


class Driver(PlantBinding, Protocol):
    driver_id: ClassVar[str]
    kind: ClassVar[str]
    login_kind: ClassVar[str | None]
    LEVER_SETS: ClassVar[tuple[str, ...]]
    IDENTITY_PARAMETERS: ClassVar[tuple[str, ...]]
    poll_seconds: float
    #: Hebelsatz, den der Treiber bedient (nicht die Beschreibung aus der Konfiguration; Regel-4-Abgleich im Host).
    lever_set_id: str

    def signals(self) -> Mapping[str, str]: ...
    def read_signal(self, role: str) -> float | str: ...
    def poll_once(self) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def login_begin(self, params: dict) -> dict: ...
    def login_finish(self, params: dict) -> None: ...
    def erkennung(self) -> dict: ...
    def inventory_sample(self) -> dict: ...
    def inventory(self, hours: int, samples: list[dict]) -> dict: ...
    def forget_credentials(self) -> None: ...
