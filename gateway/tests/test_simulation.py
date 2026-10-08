import json

import pytest

from smartheat_gateway.drivers.base import DriverError
from smartheat_gateway.drivers.registry import create
from smartheat_gateway.paths import Paths
from smartheat_gateway.quota import QuotaExhausted


@pytest.fixture
def paths(data_dir):
    return Paths(data_dir)


def _sim(paths, clock, **parameter):
    driver = create("simulation", {"lever_set": "viessmann_vicare", **parameter}, paths, clock=clock, writer=True)
    driver.poll_once()
    return driver


def test_write_is_read_back_and_counted(paths, clock):
    driver = _sim(paths, clock)
    before = driver.physical_writes
    driver.write("curve", 1.2)
    assert driver.read("curve") == 1.2
    assert driver.physical_writes == before + 1
    assert json.loads((paths.sim_dir / "plant.json").read_text())["hebel"]["curve"] == 1.2


def test_settle_delay_shows_the_old_value(paths, clock):
    walls = [1000.0]
    driver = create("simulation", {"settle_delay": 60}, paths, clock=clock, writer=True, wall=lambda: walls[0])
    driver.poll_once()
    old = driver.read("curve")
    driver.write("curve", old + 0.2)
    driver.poll_once()
    assert driver.read("curve") == old
    walls[0] += 61
    driver.poll_once()
    assert driver.read("curve") == pytest.approx(old + 0.2)


def test_stale_cache_raises(paths, clock):
    driver = _sim(paths, clock)
    clock.advance(3 * driver.poll_seconds + 1)
    with pytest.raises(ValueError, match="veraltet"):
        driver.read("curve")


def test_fault_injection(paths, clock):
    driver = _sim(paths, clock)
    control = paths.sim_dir / "control.json"
    control.write_text(json.dumps({"write_fail": ["level"]}))
    driver.poll_once()
    with pytest.raises(RuntimeError):
        driver.write("level", 1.0)
    control.write_text(json.dumps({"quota_exhausted": True}))
    driver.poll_once()
    with pytest.raises(QuotaExhausted):
        driver.write("curve", 1.1)
    control.write_text(json.dumps({"offline": True}))
    driver.poll_once()
    with pytest.raises(ConnectionError):
        driver.read("curve")
    control.write_text(json.dumps({"token_expired": True}))
    with pytest.raises(DriverError) as error:
        driver.probe()
    assert error.value.grund == "nicht_angemeldet"


def test_probe_offers_one_candidate_and_rejects_the_other(paths, clock):
    result = _sim(paths, clock).probe()
    ok, rejected = result["kandidaten"]
    assert ok["lever_set"] == "viessmann_vicare" and ok["ablehnung"] is None
    assert rejected["lever_set"] is None and rejected["ablehnung"]["grund"] == "hebel_fehlt"


def test_house_model_moves_towards_the_equilibrium(paths, clock):
    driver = _sim(paths, clock, raum_start=15.0)
    start = driver.read_signal("room_temperature")
    clock.advance(3600)
    driver.poll_once()
    assert driver.read_signal("room_temperature") > start


def test_agent_instance_does_not_create_the_plant_file(paths, clock):
    agent = create("simulation", {}, paths, clock=clock, writer=False)
    agent.probe()
    assert not (paths.sim_dir / "plant.json").exists()


def test_generator_hours_rise_and_stand_still_without_heat(paths, clock):
    # Audit 4 P-B: ohne Liefer-Signal lernt der Server nicht (E5); keine_waerme bildet einen Waermeausfall nach
    driver = _sim(paths, clock)
    assert "generator_hours" in driver.signals()
    first = driver.read_signal("generator_hours")
    clock.advance(1800)
    driver.poll_once()
    second = driver.read_signal("generator_hours")
    assert second > first
    (paths.sim_dir / "control.json").write_text(json.dumps({"keine_waerme": True}))
    clock.advance(1800)
    driver.poll_once()
    assert driver.read_signal("generator_hours") == second
    assert driver.read_signal("flow_temperature") == 29.5
