"""Treiber-Registry (Spec SHG G2 4.1). Die Server-Seite (G3 shg_catalog) nennt dieselben IDs (Contract-Check)."""
import json
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


def same_plant(old: dict | None, new: dict | None) -> bool:
    """Gleicher Treiber und gleiche identifizierende Parameter (Audit 4, A4-08; Server: shg_catalog.same_plant)."""
    if not isinstance(old, dict) or not isinstance(new, dict) or old.get("id") != new.get("id"):
        return False
    driver = DRIVERS.get(new.get("id"))
    old_p, new_p = old.get("parameter"), new.get("parameter")
    if driver is None or not isinstance(old_p, dict) or not isinstance(new_p, dict):
        return False
    return all(old_p.get(key) == new_p.get(key) for key in driver.IDENTITY_PARAMETERS)


def plant_id(spec: dict) -> str:
    """Kennung der Anlage aus Treiber und identifizierenden Parametern (Loaded.plant_id)."""
    driver = DRIVERS[spec["id"]]
    parameter = spec.get("parameter") or {}
    return json.dumps({"id": spec["id"], **{key: parameter.get(key) for key in driver.IDENTITY_PARAMETERS}},
                      sort_keys=True)
