"""Treiber-Registry. Die Server-Seite (G3 shg_catalog) nennt dieselben IDs (Contract-Check)."""
import time

from smartheat_core import kennung
from smartheat_core.binding import BindingDescription
from smartheat_gateway.drivers.base import Driver
from smartheat_gateway.drivers.simulation import SimulationDriver
from smartheat_gateway.drivers.vicare_cloud.driver import ViCareCloudDriver
from smartheat_gateway.paths import Paths

DRIVERS: dict[str, type] = {
    SimulationDriver.driver_id: SimulationDriver, ViCareCloudDriver.driver_id: ViCareCloudDriver,
}


def driver_ids() -> tuple[str, ...]:
    return tuple(sorted(DRIVERS))


def create(
    driver_id: str, parameter: dict, paths: Paths, *, clock=time.monotonic, writer: bool = False,
    description: BindingDescription | None = None, **extra,
) -> Driver:
    """writer=True nur in der Laufzeit (ein Schreiber je Datei); die Befehle des Geraetekerns lesen. description: die
    Bindungsbeschreibung aus der Konfiguration; Erkennung, Inventur und Vergessen laufen ohne.
    KeyError bei unbekannter ID."""
    return DRIVERS[driver_id](parameter, paths, clock=clock, writer=writer, description=description, **extra)


def plant_id(spec: dict) -> str:
    """Kennung des Heizkreises aus Treiber und Parametern (Loaded.plant_id): `{hersteller, anlage, nummer}` ohne
    Treiber-ID und Weg. KennungFehler, wenn die Parameter die Anlage nicht benennen."""
    return kennung.aus_treiber(spec["id"], spec.get("parameter") or {})
