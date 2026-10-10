"""Hostneutrale, gepruefte Konfiguration der Laufzeit (Spec SHG 3.3). Der HA-Host fuellt sie aus options.json
(heizungsbruecke.config.runtime_config), das SHG aus der Laufzeit-Konfiguration des Agenten, das Gateway ab 0.6.0 aus
dem Konfigurationsdokument (dokument_config, Spec 5b 5.2)."""
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from smartheat_core.levers import LeverSet
from smartheat_core.safety import LocalSafety
from smartheat_transport.descriptor import Credential, Descriptor

BATTERY_PERCENT = "prozent"
BATTERY_LOW_FLAG = "niedrig_flag"


@dataclass(frozen=True)
class BatteryRef:
    """Batterie-Signal eines Fuehlers/Thermostats (Spec SHG G2 2.2): `art` legt der Host fest (HA: aus dem Praefix
    binary_sensor., Gateway: aus den Zigbee-Faehigkeiten); die Laufzeit liest kein Praefix."""
    ref: str
    art: str  # BATTERY_PERCENT oder BATTERY_LOW_FLAG


@dataclass(frozen=True)
class BootInfo:
    """Was vor der Konfigurationspruefung feststeht (Plan SHG G1, Praezisierung 3): Status und Meldungen arbeiten schon,
    bevor die Konfiguration geprueft ist. Der Host liest es tolerant und wirft nie."""
    configured: bool
    signed_off: bool
    tenant_id: str | None
    setup_id: str | None
    lever_set: LeverSet
    client_version: str
    notify_hints_off: tuple[str, ...]
    local_check_interval: float
    backup_path: Path
    failsafe_path: Path


@dataclass(frozen=True)
class RuntimeConfig:
    tenant_id: str
    setup_id: str | None
    lever_set_id: str
    local_safety: LocalSafety
    # Optionen-Pfad (Add-on bis 5c): Transport, Zugang und Abo-Abruf. Im Geraete-Pfad (Spec 5b 5.2) None: die
    # MQTT-Verbindung haelt smartheat_device (app.start mqtt_factory), den Abo-Status liefert abo_source.
    descriptor: Descriptor | None
    credential: Credential | None
    installation_token: str | None
    accounts_api_base_url: str | None
    daily_trigger_time: str | None
    local_check_interval: float
    telemetry_interval: float
    notify_hints_off: tuple[str, ...]
    # Referenzen der SignalSource: Raumfuehler des Referenzraums und Batterie-Signale der Fuehler/Thermostate.
    room_sensor_refs: tuple[str, ...]
    battery_refs: tuple[BatteryRef, ...]
    # Datei der Abo-inaktiv-Frist (entitlement.py).
    entitlement_path: Path
    # Spec 5b (Plan D1): Abo-Status aus dem Konfigurationsdokument (entitlement.ACTIVE/INACTIVE/UNKNOWN); None = Abruf
    # GET /tenants/<id>/status mit dem Installations-Token.
    abo_source: Callable[[], str] | None = None
