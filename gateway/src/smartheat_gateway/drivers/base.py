"""Treiber-Protokoll (Spec SHG G2 4.1): ein Treiber IST ein PlantBinding aus smartheat_core mit derselben
BindingDescription wie der HA-Pfad, plus Signale, Abfrage-Thread, Login, Probe und Inventur. Lesen kommt aus dem Cache;
aelter als CACHE_MAX_POLLS x poll_seconds (monotone Uhr) -> ValueError, die Datenfehler-Logik der Laufzeit greift."""
from collections.abc import Mapping
from typing import ClassVar, Protocol

from smartheat_core.binding import PlantBinding
from smartheat_gateway.quota import QuotaSpec
from smartheat_runtime.roles import LEVER_ROLES

__all__ = [
    "CACHE_MAX_POLLS", "KIND_CLOUD", "KIND_LOCAL", "LEVER_ROLES", "LOGIN_OAUTH", "LOGIN_PASSWORD", "Driver",
    "DriverError",
]

KIND_CLOUD = "cloud"
KIND_LOCAL = "local"
LOGIN_OAUTH = "oauth_pkce"
LOGIN_PASSWORD = "passwort_verschluesselt"
CACHE_MAX_POLLS = 3


class DriverError(RuntimeError):
    """Fehler mit Vertragsgrund (agent/wire.py): nicht_angemeldet, anlage_nicht_erreichbar, login_fehlgeschlagen,
    login_nicht_noetig, kontingent_erschoepft."""

    def __init__(self, grund: str, text: str) -> None:
        super().__init__(text)
        self.grund = grund
        self.text = text


class Driver(PlantBinding, Protocol):
    driver_id: ClassVar[str]
    kind: ClassVar[str]
    login_kind: ClassVar[str | None]
    REJECTION_REASONS: ClassVar[tuple[str, ...]]
    quota: QuotaSpec | None
    poll_seconds: float

    def signals(self) -> Mapping[str, str]: ...
    def read_signal(self, role: str) -> float | str: ...
    def poll_once(self) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def login_begin(self, params: dict) -> dict: ...
    def login_finish(self, params: dict) -> None: ...
    def probe(self) -> dict: ...
    def inventory(self, hours: int) -> dict: ...
