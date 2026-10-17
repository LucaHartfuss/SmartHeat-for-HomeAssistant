import json

import pytest
from bindung_beispiele import VIESSMANN_VICARE_BINDING

from smartheat_device import wire
from smartheat_gateway import safety_warnings
from smartheat_gateway.drivers.base import DriverError
from smartheat_gateway.drivers.registry import create
from smartheat_gateway.drivers.simulation import SIM_SCHRITTE
from smartheat_gateway.paths import Paths
from smartheat_gateway.quota import QuotaExhausted


@pytest.fixture
def paths(data_dir):
    return Paths(data_dir)


def _sim(paths, clock, **parameter):
    driver = create("simulation", {"lever_set": "viessmann_vicare", **parameter}, paths, clock=clock, writer=True,
                    description=VIESSMANN_VICARE_BINDING)
    driver.poll_once()
    return driver


@pytest.mark.parametrize("wert", [float("nan"), float("inf"), float("-inf"), 10**400, -10**400],
                         ids=["nan", "inf", "minus-inf", "gross-positiv", "gross-negativ"])
@pytest.mark.parametrize("schluessel", ["poll_seconds", "settle_delay", "aussen_mittel", "aussen_amplitude", "rauschen",
                                        "raum_start"])
def test_a_numeric_parameter_that_is_not_finite_is_refused(paths, clock, schluessel, wert):
    with pytest.raises(ValueError):
        create("simulation", {"lever_set": "viessmann_vicare", schluessel: wert}, paths, clock=clock, writer=True)


def test_write_is_read_back_and_counted(paths, clock):
    driver = _sim(paths, clock)
    before = driver.physical_writes
    driver.write("curve", 1.2)
    assert driver.read("curve") == 1.2
    assert driver.physical_writes == before + 1
    assert json.loads((paths.sim_dir / "plant.json").read_text())["hebel"]["curve"] == 1.2


def test_settle_delay_shows_the_old_value(paths, clock):
    walls = [1000.0]
    driver = create("simulation", {"settle_delay": 60}, paths, clock=clock, writer=True, wall=lambda: walls[0],
                    description=VIESSMANN_VICARE_BINDING)
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
        driver.erkennung()
    assert error.value.grund == "nicht_angemeldet"


def test_erkennung_hat_zwei_features_und_eine_kennung_ohne_heizkreis(paths, clock):
    daten = _sim(paths, clock).erkennung()
    assert tuple(daten) == wire.INVENTUR_GATEWAY_FIELDS and daten["treiber"] == "simulation"
    (anlage,) = daten["anlagen"]
    assert tuple(anlage) == wire.INVENTUR_ANLAGE_FIELDS
    assert anlage["kennung"] == {"lever_set": "viessmann_vicare", "settle_delay": 0.0, "poll_seconds": 60.0}
    schreibbar, nicht = daten["features"]
    assert (schreibbar["feature"], nicht["feature"]) == ("simulation.heizkreis.0", "simulation.heizkreis.1")
    assert all(tuple(f) == ("anlage", "feature", "properties", "commands") and f["anlage"] == 0 for f in (schreibbar, nicht))
    assert schreibbar["properties"]["schreibbar"] == {"value": True}
    assert nicht["properties"]["schreibbar"] == {"value": False} and "hebel" not in nicht["properties"]
    hebel = schreibbar["properties"]["hebel"]["value"]
    assert set(hebel) == {"curve", "level", "room_setpoint"} and hebel["curve"]["schritt"] == 0.1
    assert schreibbar["properties"]["erzeuger_typ"] == {"value": "gastherme"}
    assert "outdoor_temp" in schreibbar["properties"]["signale"]["value"]


@pytest.mark.parametrize("hebelsatz", sorted(SIM_SCHRITTE))
def test_erkennung_warnungen_kommen_aus_dem_hebelstand_des_ersten_kreises(paths, clock, hebelsatz):
    daten = _sim(paths, clock, lever_set=hebelsatz).erkennung()
    hebel = daten["features"][0]["properties"]["hebel"]["value"]
    warnungen = daten["anlagen"][0]["sicherheitswarnungen"]
    assert warnungen == {"0": safety_warnings.compute({"lever_set": hebelsatz, "hebel": hebel})} and warnungen["0"]


def test_erkennung_mit_abgelaufener_anmeldung_ist_nicht_angemeldet(paths, clock):
    driver = _sim(paths, clock)
    (paths.sim_dir / "control.json").write_text(json.dumps({"token_expired": True}))
    with pytest.raises(DriverError) as error:
        driver.erkennung()
    assert error.value.grund == "nicht_angemeldet"


def test_erkennung_of_a_reader_instance_does_not_create_the_plant_file(paths, clock):
    create("simulation", {}, paths, clock=clock, writer=False).erkennung()
    assert not (paths.sim_dir / "plant.json").exists()


def test_house_model_moves_towards_the_equilibrium(paths, clock):
    driver = _sim(paths, clock, raum_start=15.0)
    start = driver.read_signal("room_temperature")
    clock.advance(3600)
    driver.poll_once()
    assert driver.read_signal("room_temperature") > start


def test_reader_instance_does_not_create_the_plant_file(paths, clock):
    reader = create("simulation", {}, paths, clock=clock, writer=False)
    reader.erkennung()
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


def test_aux_zulaessig_takes_short_texts_for_aux_names_only(paths, clock):
    driver = _sim(paths, clock)
    assert driver.aux_zulaessig("mode_select", "eco") is True
    assert driver.aux_zulaessig("mode_select", "") is False
    assert driver.aux_zulaessig("mode_select", "x" * 65) is False
    assert driver.aux_zulaessig("curve", "eco") is False
