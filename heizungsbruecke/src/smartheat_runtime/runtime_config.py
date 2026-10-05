"""Hostneutrale, gepruefte Konfiguration der Laufzeit (Spec SHG 3.3). Der HA-Host fuellt sie aus options.json
(heizungsbruecke.config.runtime_config), das SHG aus der Laufzeit-Konfiguration des Agenten."""
from dataclasses import dataclass
from pathlib import Path

from smartheat_core.safety import LocalSafety
from smartheat_transport.descriptor import Credential, Descriptor


@dataclass(frozen=True)
class RuntimeConfig:
    tenant_id: str
    setup_id: str | None
    lever_set_id: str
    local_safety: LocalSafety
    descriptor: Descriptor
    credential: Credential
    installation_token: str
    accounts_api_base_url: str
    daily_trigger_time: str | None
    local_check_interval: float
    telemetry_interval: float
    notify_hints_off: tuple[str, ...]
    # Referenzen der SignalSource: Raumfuehler des Referenzraums und Batterie-Signale der Fuehler/Thermostate.
    room_sensor_refs: tuple[str, ...]
    battery_refs: tuple[str, ...]
    # Datei der Abo-inaktiv-Frist (entitlement.py).
    entitlement_path: Path
