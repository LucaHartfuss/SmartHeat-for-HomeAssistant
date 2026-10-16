"""Treiber-Registry (Spec SHG G2 4.1). Die Server-Seite (G3 shg_catalog) nennt dieselben IDs (Contract-Check)."""
import json
import time

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
    Bindungsbeschreibung aus der Konfiguration (Spec Stufe 3 S4b); Erkennung, Inventur und Vergessen laufen ohne.
    KeyError bei unbekannter ID."""
    return DRIVERS[driver_id](parameter, paths, clock=clock, writer=writer, description=description, **extra)


def plant_id(spec: dict) -> str:
    """Kennung der Anlage aus Treiber und identifizierenden Parametern (Loaded.plant_id)."""
    driver = DRIVERS[spec["id"]]
    parameter = spec.get("parameter") or {}
    return json.dumps({"id": spec["id"], **{key: parameter.get(key) for key in driver.IDENTITY_PARAMETERS}},
                      sort_keys=True)
