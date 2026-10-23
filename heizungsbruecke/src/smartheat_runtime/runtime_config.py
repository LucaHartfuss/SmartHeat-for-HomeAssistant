"""Hostneutrale, gepruefte Konfiguration der Laufzeit. Beide Hosts (HA-Add-on und Gateway) fuellen sie
ueber den Geraetekern aus dem Konfigurationsdokument des Servers (dokument_config.runtime_config)."""
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from smartheat_core.levers import LeverSet
from smartheat_core.safety import LocalSafety

BATTERY_PERCENT = "prozent"
BATTERY_LOW_FLAG = "niedrig_flag"


@dataclass(frozen=True)
class BatteryRef:
    """Batterie-Signal eines Fuehlers/Thermostats: `art` legt der Host fest (HA: aus dem Praefix
    binary_sensor., Gateway: aus den Zigbee-Faehigkeiten); die Laufzeit liest kein Praefix."""
    ref: str
    art: str  # BATTERY_PERCENT oder BATTERY_LOW_FLAG


@dataclass(frozen=True)
class BootInfo:
    """Was vor der Konfigurationspruefung feststeht (Praezisierung 3): Status und Meldungen arbeiten schon,
    bevor die Konfiguration geprueft ist. Der Host liest es tolerant und wirft nie."""
    configured: bool
    signed_off: bool
    tenant_id: str | None
    lever_set: LeverSet
    client_version: str
    notify_hints_off: tuple[str, ...]
    local_check_interval: float
    backup_path: Path
    failsafe_path: Path
    # Aufraeum-Markierung des Hosts; nach gelungenem Zuruecksetzen erledigt die Laufzeit
    # sie und startet neu (None: der Host hat keine).
    aufraeumen_path: Path | None = None


@dataclass(frozen=True)
class RuntimeConfig:
    tenant_id: str
    lever_set_id: str
    local_safety: LocalSafety
    daily_trigger_time: str | None
    local_check_interval: float
    telemetry_interval: float
    notify_hints_off: tuple[str, ...]
    # Referenzen der SignalSource: Raumfuehler des Referenzraums und Batterie-Signale der Fuehler/Thermostate.
    room_sensor_refs: tuple[str, ...]
    battery_refs: tuple[BatteryRef, ...]
    # Datei der Abo-inaktiv-Frist (entitlement.py).
    entitlement_path: Path
    # Abo-Status aus dem Konfigurationsdokument (entitlement.ACTIVE/INACTIVE/UNKNOWN). Die MQTT-
    # Verbindung haelt smartheat_device (app.start mqtt_factory), die Laufzeit fragt keinen Server selbst.
    abo_source: Callable[[], str]
