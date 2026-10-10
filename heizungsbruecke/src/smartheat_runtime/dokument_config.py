"""Laufzeit aus dem Konfigurationsdokument (Spec 5b 5.2, Plan D1): sobald ein Host smartheat_device nutzt (Gateway ab
0.6.0, HA-Add-on ab 5c), liest die Laufzeit Anlage, Hebelsatz, Verteilsystem, Raumfuehler, Tagestick, Abo und
Intervalle aus dem Konfigurationsdokument statt aus Optionen. Transport und Zugang haelt der Geraetekern (eine
MQTT-Verbindung), der Abo-Status steht im Dokument (Plan S1, Praezisierung abo; die 30-Tage-Frist bleibt lokal).
Die Anlage bindet den lokalen Zustand (A4-08): setup_id = Anlage, eine andere Anlage beginnt mit leerem Zustand, eine
neue Konfiguration derselben Anlage nicht. Feldnamen = smartheat_device.wire.KONFIGURATION_FIELDS (die Laufzeit
importiert den Geraetekern nicht; ein Test haelt beide gleich). Die Bindung (Treiber, Thermostat) liest der Host."""
from collections.abc import Callable, Mapping
from pathlib import Path

from smartheat_core.binding import BINDINGS
from smartheat_core.safety import resolve_local_safety
from smartheat_runtime import entitlement, options
from smartheat_runtime.options import ConfigError
from smartheat_runtime.runtime_config import BootInfo, RuntimeConfig
from smartheat_runtime.windows import validate_daily_trigger_time

FELDER = ("anlage", "profil", "hebelsatz", "verteilsystem", "bindung", "raumfuehler", "tagestick", "abo", "intervalle")
#: Aendert sich eines davon, startet der Host die Laufzeit neu (software, mindest_software und profil nicht).
LAUFZEIT_FELDER = ("anlage", "hebelsatz", "verteilsystem", "bindung", "raumfuehler", "tagestick", "abo", "intervalle")
ABO = {"aktiv": entitlement.ACTIVE, "inaktiv": entitlement.INACTIVE}


def eingerichtet(konfiguration) -> bool:
    return isinstance(konfiguration, Mapping) and isinstance(konfiguration.get("anlage"), str) and bool(
        konfiguration["anlage"])


def abo_status(konfiguration) -> str:
    if not eingerichtet(konfiguration):
        return entitlement.UNKNOWN
    return ABO.get(konfiguration.get("abo"), entitlement.UNKNOWN)


def _intervall(konfiguration, key: str, default: float) -> float:
    werte = konfiguration.get("intervalle") if isinstance(konfiguration, Mapping) else None
    value = werte.get(key) if isinstance(werte, Mapping) else None
    return value if isinstance(value, int | float) and not isinstance(value, bool) else default


def boot_info(konfiguration, *, abgemeldet: bool, client_version: str, backup_path: Path,
              failsafe_path: Path) -> BootInfo:
    """Wirft nie (wie die BootInfo der Hosts): ein unbekannter Hebelsatz faellt fuer das Statusereignis auf den
    Standard zurueck; runtime_config meldet ihn danach als Konfigurationsfehler."""
    anlage = konfiguration["anlage"] if eingerichtet(konfiguration) else None
    hebelsatz = konfiguration.get("hebelsatz") if isinstance(konfiguration, Mapping) else None
    binding = BINDINGS.get(hebelsatz) if isinstance(hebelsatz, str) else None
    lever_set = (binding or BINDINGS[options.DEFAULT_LEVER_SET]).lever_set
    return BootInfo(
        configured=anlage is not None and not abgemeldet, signed_off=anlage is not None and abgemeldet,
        tenant_id=anlage, setup_id=anlage, lever_set=lever_set, client_version=client_version, notify_hints_off=(),
        local_check_interval=_intervall(konfiguration, "pruefung_s", options.DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS),
        backup_path=backup_path, failsafe_path=failsafe_path,
    )


def runtime_config(konfiguration, *, entitlement_path: Path, abo_source: Callable[[], str]) -> RuntimeConfig:
    """Wirft ConfigError (der Host macht daraus StartFailure); config_check hat das Dokument schon geprueft."""
    if not eingerichtet(konfiguration):
        raise ConfigError("Das Gerät ist nicht eingerichtet")
    hebelsatz = konfiguration.get("hebelsatz")
    try:
        safety = resolve_local_safety(hebelsatz, konfiguration.get("verteilsystem"))
        daily = validate_daily_trigger_time(konfiguration.get("tagestick"))
    except ValueError as error:
        raise ConfigError(f"Konfiguration: {error}") from None
    sensors = konfiguration.get("raumfuehler")
    if not isinstance(sensors, list) or not sensors or not all(isinstance(ref, str) for ref in sensors):
        raise ConfigError("Konfiguration: keine Raumfühler")
    return RuntimeConfig(
        tenant_id=konfiguration["anlage"], setup_id=konfiguration["anlage"], lever_set_id=hebelsatz,
        local_safety=safety, descriptor=None, credential=None, installation_token=None, accounts_api_base_url=None,
        daily_trigger_time=daily,
        local_check_interval=_intervall(konfiguration, "pruefung_s", options.DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS),
        telemetry_interval=_intervall(konfiguration, "telemetrie_s", options.DEFAULT_TELEMETRY_INTERVAL_SECONDS),
        notify_hints_off=(), room_sensor_refs=tuple(sensors), battery_refs=(), entitlement_path=entitlement_path,
        abo_source=abo_source,
    )


def laufzeit_relevant(alt, neu) -> bool:
    alt = alt if isinstance(alt, Mapping) else {}
    neu = neu if isinstance(neu, Mapping) else {}
    return any(alt.get(key) != neu.get(key) for key in LAUFZEIT_FELDER)
