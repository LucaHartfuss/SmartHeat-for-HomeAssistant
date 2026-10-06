"""Treiber-Konformitaet (Spec SHG G2 7.3): jeder Treiber der Registry erfuellt PlantBinding mit derselben
BindingDescription wie der HA-Pfad, liefert die Pflicht-Rollen seines Hebelsatzes und ein Probe-Ergebnis nach dem
Vertrag. G4 ergaenzt PARAMETERS um seine Treiber."""
import pytest

from smartheat_core.binding import BINDINGS
from smartheat_gateway.agent import wire
from smartheat_gateway.drivers.base import LEVER_ROLES
from smartheat_gateway.drivers.registry import DRIVERS, create
from smartheat_gateway.paths import Paths
from smartheat_runtime.roles import REQUIRED_ROLES_BY_LEVER_SET

# Treiber -> Parametersaetze, unter denen er im Test laeuft (ein Satz je Hebelsatz, den er bedienen kann).
PARAMETERS = {"simulation": [{"lever_set": lever_set} for lever_set in BINDINGS]}
CASES = [(driver_id, parameter) for driver_id in DRIVERS for parameter in PARAMETERS[driver_id]]


@pytest.fixture
def driver(request, data_dir, clock):
    driver_id, parameter = request.param
    instance = create(driver_id, parameter, Paths(data_dir), clock=clock, writer=True)
    instance.poll_once()
    return instance


def test_every_registered_driver_has_parameters():
    assert set(PARAMETERS) == set(DRIVERS)


def test_vaillant_is_among_the_conformance_cases():
    assert "vaillant_vrc720" in {parameter["lever_set"] for _, parameter in CASES}


@pytest.mark.parametrize("driver", CASES, indirect=True)
def test_binding_description_is_the_one_of_the_ha_path(driver):
    standard = BINDINGS[driver.description.lever_set.id]
    assert dict(driver.description.steps) == dict(standard.steps)
    assert dict(driver.description.enforce_tolerance) == dict(standard.enforce_tolerance)
    assert driver.description.restore_originals == standard.restore_originals


@pytest.mark.parametrize("driver", CASES, indirect=True)
def test_levers_read_write_and_aux(driver):
    for lever in driver.description.lever_set.levers:
        assert driver.has(lever)
        assert driver.ref(lever) == f"treiber:{LEVER_ROLES[lever]}"
        value = driver.read(lever)
        driver.write(lever, value)
        assert driver.read(lever) == value
    aux = driver.read_aux()
    assert set(aux) == set(driver.description.aux_originals)
    driver.restore_aux(aux)


@pytest.mark.parametrize("driver", CASES, indirect=True)
def test_signals_cover_the_required_roles_except_the_room(driver):
    required = set(REQUIRED_ROLES_BY_LEVER_SET[driver.description.lever_set.id]) - {"room_actual", "room_target"}
    assert required <= set(driver.signals())
    assert all(ref == f"treiber:{role}" for role, ref in driver.signals().items())


@pytest.mark.parametrize("driver", CASES, indirect=True)
def test_probe_follows_the_contract(driver):
    result = driver.probe()
    assert tuple(result) == wire.PROBE_FIELDS
    for candidate in result["kandidaten"]:
        assert tuple(candidate) == wire.PROBE_CANDIDATE_FIELDS
        assert (candidate["lever_set"] is None) != (candidate["ablehnung"] is None)
        if candidate["ablehnung"] is not None:
            assert candidate["ablehnung"]["grund"] in type(driver).REJECTION_REASONS
        for lever in candidate["hebel"].values():
            assert tuple(lever) == wire.PROBE_LEVER_FIELDS
