"""Treiber-Registry (Spec SHG G2 4.1). Die Server-Seite (G3 shg_catalog) nennt dieselben IDs (Contract-Check)."""
import time

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
    driver_id: str, parameter: dict, paths: Paths, *, clock=time.monotonic, writer: bool = False, **extra,
) -> Driver:
    """writer=True nur in der Laufzeit (ein Schreiber je Datei); der Agent liest. KeyError bei unbekannter ID."""
    return DRIVERS[driver_id](parameter, paths, clock=clock, writer=writer, **extra)
