import json
import math
import random

import pytest
from bindung_beispiele import BINDINGS, VIESSMANN_VICARE_BINDING

from smartheat_core import hausmodell, zeitquelle
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


# Entscheidung 22: poll_seconds der Simulation liegt zwischen 1 und 86400 s, beide Grenzen eingeschlossen; kein bool,
# kein Text. Dieselben Literale pruefen der Server (test_gateway_erkennung.py) und der Root (test_treibergrenzen_gleichlauf.py).
@pytest.mark.parametrize(("wert", "angenommen"), [
    (0.999, False), (1, True), (1.0, True), (60, True), (86400, True), (86400.0, True), (86400.5, False),
    (86401, False), (0, False), (-1, False), (1e300, False), (float("nan"), False), (float("inf"), False),
    (float("-inf"), False), (True, False), (False, False), ("60", False)])
def test_the_poll_interval_of_the_simulation_lies_between_1_and_86400_seconds(paths, clock, wert, angenommen):
    parameter = {"lever_set": "viessmann_vicare", "poll_seconds": wert}
    if angenommen:
        assert create("simulation", parameter, paths, clock=clock, writer=True).poll_seconds == float(wert)
    else:
        with pytest.raises(ValueError, match="zwischen 1 und 86400 s"):
            create("simulation", parameter, paths, clock=clock, writer=True)


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


@pytest.mark.parametrize(("poll", "alter_s", "frisch"), [
    (60, 179, True), (60, 180, True), (60, 181, False),
    (1, 2, True), (1, 3, True), (1, 4, False)])
def test_the_cache_is_fresh_up_to_exactly_three_poll_intervals(paths, clock, poll, alter_s, frisch):
    driver = _sim(paths, clock, poll_seconds=poll)
    clock.advance(alter_s)
    if frisch:
        assert driver.read("curve") is not None
    else:
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


# --- Hausmodell-Kern: der Treiber ruft ihn je 5-min-Schritt der monotonen Uhr ---

WALL = 12 * 3600.0  # feste Wanduhr; mit aussen_amplitude 0 ist die Aussentemperatur dann aussen_mittel
AUSSEN = 5.0
SCHRITT = hausmodell.DT
# Abbildung Hebel -> Kern je Hebelsatz: (geschriebene Hebel, Erzeuger (a, b), (Steigung, Verschiebung, Heizgrenze), Mindestvorlauf).
# Heizgrenze 16 und Steigung 1 gelten, wo der Hebelsatz den Hebel nicht hat.
ABBILDUNG = {
    "vaillant_vrc720": ({"curve": 1.4, "room_setpoint": 21.0, "heat_limit": 15.0, "min_flow": 22.0},
                        (2.54, 0.78), (1.4, 21.0, 15.0), 22.0),
    "weishaupt_wwp": ({"curve": 1.2, "room_setpoint": 21.5, "heat_limit": 14.0},
                      (1.0, 1.0), (1.2, 21.5, 14.0), 21.5),
    "weishaupt_wwp_basis": ({"room_setpoint": 22.0}, (1.0, 1.0), (1.0, 22.0, 16.0), 22.0),
    "viessmann_vicare": ({"curve": 1.2, "room_setpoint": 19.0, "level": 3.0}, (1.0, 1.0), (1.2, 22.0, 16.0), 19.0),
}


def _sim_satz(paths, clock, hebelsatz, hebel=None, **parameter):
    driver = create("simulation", {"lever_set": hebelsatz, "aussen_amplitude": 0.0, "aussen_mittel": AUSSEN, **parameter},
                    paths, clock=clock, writer=True, description=BINDINGS[hebelsatz], wall=lambda: WALL)
    driver.poll_once()
    for name, wert in (hebel or {}).items():
        driver.write(name, wert)
    return driver


def _kern(erzeuger, hebel, ziel, schritte, *, raum=20.0, liefert=True, seed=None, aussen=AUSSEN):
    """Direktlauf des Kerns mit den Eingaben des Treibers (Stunde fest ausserhalb der Warmwasserpausen)."""
    zustand = hausmodell.Anlagenzustand(room=raum, burner_hours=1000.0)
    rng = None if seed is None else random.Random(seed)
    letzter = None
    for _ in range(schritte):
        letzter = hausmodell.anlage_schritt(
            zustand, hausmodell.HOUSES["H1"], hausmodell.Potenzgesetz(*erzeuger), hebel, aussen, 0.0, 12.0, ziel,
            hausmodell.Betriebsflags(liefert=liefert), rng)
    return zustand, letzter


@pytest.mark.parametrize("hebelsatz", sorted(ABBILDUNG))
def test_driver_run_equals_direct_run_of_the_core(paths, clock, hebelsatz):
    geschrieben, erzeuger, hebel, ziel = ABBILDUNG[hebelsatz]
    driver = _sim_satz(paths, clock, hebelsatz, geschrieben)
    clock.advance(12 * SCHRITT)
    driver.poll_once()
    zustand, letzter = _kern(erzeuger, hebel, ziel, 12)
    assert zustand.heating and letzter.geliefert
    assert driver.read_signal("room_temperature") == round(zustand.room, 2)
    assert driver.read_signal("generator_hours") == round(zustand.burner_hours, 3)
    assert driver.read_signal("flow_setpoint") == round(letzter.flow, 1)
    assert driver.read_signal("flow_temperature") == round(letzter.flow, 1)
    assert driver.read_signal("outdoor_temp") == AUSSEN


# Faelle, in denen der Mindestvorlauf bindet (der Vorlauf der Heizkurve liegt darunter): Hebelsatz, geschriebene Hebel, Erzeuger,
# Eingaben des Kerns, Mindestvorlauf. Ohne Hebel `min_flow` gilt das Raum-Soll; fehlt er nur ungeschrieben, sein Startwert (20).
MINDESTVORLAUF = {
    "mit_min_flow": ("vaillant_vrc720", {"curve": 0.2, "room_setpoint": 21.0, "min_flow": 60.0}, (2.54, 0.78), (0.2, 21.0, 16.0), 60.0),
    "min_flow_ungeschrieben": ("vaillant_vrc720", {"curve": 0.2, "room_setpoint": 12.0}, (2.54, 0.78), (0.2, 12.0, 16.0), 20.0),
    # Das negative Niveau senkt die Verschiebung unter das Raum-Soll; ohne Hebel `min_flow` gilt das Raum-Soll.
    "ohne_min_flow": ("viessmann_vicare", {"curve": 0.2, "room_setpoint": 20.0, "level": -10.0}, (1.0, 1.0), (0.2, 10.0, 16.0), 20.0),
}


@pytest.mark.parametrize("fall", sorted(MINDESTVORLAUF))
def test_the_minimum_flow_binds_exactly_as_in_the_core(paths, clock, fall):
    hebelsatz, geschrieben, erzeuger, hebel, ziel = MINDESTVORLAUF[fall]
    kurve = hausmodell.Potenzgesetz(*erzeuger).flow(hebel[0], hebel[1], AUSSEN)
    assert kurve < ziel - 1.0  # sonst unterscheidet der Fall den Mindestvorlauf nicht
    driver = _sim_satz(paths, clock, hebelsatz, geschrieben)
    clock.advance(12 * SCHRITT)
    driver.poll_once()
    zustand, letzter = _kern(erzeuger, hebel, ziel, 12)
    assert zustand.heating and letzter.geliefert and letzter.flow == ziel
    assert driver.read_signal("flow_setpoint") == ziel
    assert driver.read_signal("flow_temperature") == ziel
    assert driver.read_signal("room_temperature") == round(zustand.room, 2)
    assert driver.read_signal("generator_hours") == round(zustand.burner_hours, 3)


def test_the_start_room_is_the_room_of_the_first_reading(paths, clock):
    driver = _sim_satz(paths, clock, "vaillant_vrc720", raum_start=17.5)
    assert driver.read_signal("room_temperature") == 17.5


def test_the_remainder_of_the_interval_is_carried_to_the_next_poll(paths, clock):
    geschrieben, erzeuger, hebel, ziel = ABBILDUNG["vaillant_vrc720"]
    driver = _sim_satz(paths, clock, "vaillant_vrc720", geschrieben)
    for gesamt, schritte in ((200, 0), (400, 1), (600, 2)):
        clock.advance(200)
        driver.poll_once()
        zustand, _ = _kern(erzeuger, hebel, ziel, schritte)
        assert driver.read_signal("room_temperature") == round(zustand.room, 2), gesamt


def test_one_poll_runs_at_most_one_day(paths, clock):
    geschrieben, erzeuger, hebel, ziel = ABBILDUNG["vaillant_vrc720"]
    driver = _sim_satz(paths, clock, "vaillant_vrc720", geschrieben)
    clock.advance(5 * 86400 + 100)
    driver.poll_once()
    zustand, _ = _kern(erzeuger, hebel, ziel, hausmodell.STEPS_PER_DAY)
    assert driver.read_signal("room_temperature") == round(zustand.room, 2)
    assert driver.read_signal("generator_hours") == round(zustand.burner_hours, 3)


@pytest.fixture
def paths_zwei(tmp_path):
    """Eine zweite, getrennte Anlage (eigene Dateien) fuer Vergleiche im selben Test."""
    (tmp_path / "zwei").mkdir()
    return Paths(tmp_path / "zwei")


def test_a_higher_curve_warms_the_room(paths, paths_zwei, clock):
    flach = _sim_satz(paths, clock, "vaillant_vrc720", {"curve": 0.5})
    steil = _sim_satz(paths_zwei, clock, "vaillant_vrc720", {"curve": 2.0})
    clock.advance(6 * 3600)
    flach.poll_once()
    steil.poll_once()
    assert steil.read_signal("room_temperature") > flach.read_signal("room_temperature") + 0.5


def test_a_heat_limit_below_the_outdoor_temperature_does_not_heat(paths, clock):
    driver = _sim_satz(paths, clock, "vaillant_vrc720", {"heat_limit": 10.0}, aussen_mittel=12.0)
    start = driver.read_signal("generator_hours")
    clock.advance(6 * 3600)
    driver.poll_once()
    assert driver.read_signal("generator_hours") == start
    assert driver.read_signal("room_temperature") < 20.0
    assert driver.read_signal("flow_temperature") == 29.5


def test_without_heat_delivery_the_room_cools_and_the_burner_stands_still(paths, clock):
    driver = _sim_satz(paths, clock, "vaillant_vrc720")
    (paths.sim_dir / "control.json").write_text(json.dumps({"keine_waerme": True}))
    clock.advance(6 * 3600)
    driver.poll_once()
    zustand, _ = _kern((2.54, 0.78), (1.0, 20.0, 16.0), 20.0, 72, liefert=False)
    assert driver.read_signal("room_temperature") == round(zustand.room, 2)
    assert driver.read_signal("room_temperature") < 20.0
    assert driver.read_signal("generator_hours") == 1000.0
    assert driver.read_signal("flow_temperature") == 29.5


@pytest.mark.parametrize("hebelsatz", sorted(ABBILDUNG))
def test_every_lever_set_runs(paths, clock, hebelsatz):
    driver = _sim_satz(paths, clock, hebelsatz)
    clock.advance(3600)
    driver.poll_once()
    assert 10.0 < driver.read_signal("room_temperature") < 30.0
    assert driver.read_signal("generator_hours") > 1000.0


def test_the_noise_follows_the_seed_and_scales_with_the_parameter(paths, paths_zwei, clock):
    geschrieben, erzeuger, hebel, ziel = ABBILDUNG["vaillant_vrc720"]
    ruhig = _sim_satz(paths, clock, "vaillant_vrc720", geschrieben)
    laut = _sim_satz(paths_zwei, clock, "vaillant_vrc720", geschrieben, rauschen=0.2, seed=3)
    clock.advance(SCHRITT)
    ruhig.poll_once()
    laut.poll_once()
    zustand, letzter = _kern(erzeuger, hebel, ziel, 1, seed=3)
    abweichung = letzter.gemessen - zustand.room
    assert ruhig.read_signal("room_temperature") == round(zustand.room, 2)
    assert laut.read_signal("room_temperature") == pytest.approx(zustand.room + 2 * abweichung, abs=0.006)


def test_reader_instance_does_not_create_the_plant_file(paths, clock):
    reader = create("simulation", {}, paths, clock=clock, writer=False)
    reader.erkennung()
    assert not (paths.sim_dir / "plant.json").exists()


def test_generator_hours_rise_and_stand_still_without_heat(paths, clock):
    # Ohne Liefer-Signal lernt der Server nicht; keine_waerme bildet einen Waermeausfall nach
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


# --- zwei Heizkreise ---

def test_jeder_heizkreis_hat_seinen_eigenen_anlagenzustand(paths, clock):
    eins = _sim(paths, clock, heizkreis=0)
    zwei = _sim(paths, clock, heizkreis=1)
    eins.write("curve", 1.7)
    assert eins.read("curve") == 1.7 and zwei.read("curve") != 1.7
    zwei.write("curve", 0.6)
    assert (paths.sim_dir / "plant.json").is_file() and (paths.sim_dir / "plant-1.json").is_file()
    assert eins.read("curve") == 1.7 and zwei.read("curve") == 0.6
    assert eins.physical_writes == 1 and zwei.physical_writes == 1


def test_ohne_heizkreis_gilt_die_datei_des_ersten_kreises(paths, clock):
    _sim(paths, clock).write("curve", 1.4)
    assert _sim(paths, clock, heizkreis=0).read("curve") == 1.4
    assert not (paths.sim_dir / "plant-1.json").exists()


def test_der_zweite_kreis_ist_nur_schreibbar_wenn_die_steuerdatei_es_verlangt(paths, clock):
    standard = _sim(paths, clock).erkennung()["features"]
    assert [f["properties"]["schreibbar"]["value"] for f in standard] == [True, False]
    paths.sim_dir.mkdir(parents=True, exist_ok=True)
    (paths.sim_dir / "control.json").write_text(json.dumps({"heizkreise": 2}))
    zwei = _sim(paths, clock).erkennung()["features"]
    assert [f["feature"] for f in zwei] == ["simulation.heizkreis.0", "simulation.heizkreis.1"]
    assert [f["properties"]["schreibbar"]["value"] for f in zwei] == [True, True]
    assert all("hebel" in f["properties"] for f in zwei)


@pytest.mark.parametrize("wert", [0, 3, True, "2", 1.5])
def test_eine_unbrauchbare_kreiszahl_der_steuerdatei_gilt_als_ein_kreis(paths, clock, wert):
    paths.sim_dir.mkdir(parents=True, exist_ok=True)
    (paths.sim_dir / "control.json").write_text(json.dumps({"heizkreise": wert}))
    features = _sim(paths, clock).erkennung()["features"]
    assert [f["properties"]["schreibbar"]["value"] for f in features] == [True, False]


@pytest.mark.parametrize("wert", [-1, 1.5, "1", True, 10**6])
def test_ein_unbrauchbarer_heizkreis_wird_abgelehnt(paths, clock, wert):
    with pytest.raises(ValueError, match="heizkreis"):
        create("simulation", {"lever_set": "viessmann_vicare", "heizkreis": wert}, paths, clock=clock)


# --- Zeitverhalten: das Haus rechnet auf der monotonen Uhr, der Tagesgang der Aussentemperatur folgt der fachlichen Zeit ---

MITTEL = 5.0
AMPLITUDE = 5.0


def _tagesgang(wall, mittel=MITTEL, amplitude=AMPLITUDE):
    """Aussentemperatur nach der Vorgabe: Mittelwert plus Sinus ueber 24 h, Nullstelle um 9 Uhr."""
    stunden = (wall / 3600.0) % 24
    return mittel + amplitude * math.sin(2 * math.pi * (stunden - 9) / 24)


def _sim_zeit(paths, clock, walls, hebelsatz="vaillant_vrc720", hebel=None, **parameter):
    """Treiber mit steuerbarer Wanduhr (`walls[0]`) und dem Tagesgang (Mittel 5, Amplitude 5)."""
    driver = create("simulation", {"lever_set": hebelsatz, "aussen_amplitude": AMPLITUDE, "aussen_mittel": MITTEL, **parameter},
                    paths, clock=clock, writer=True, description=BINDINGS[hebelsatz], wall=lambda: walls[0])
    driver.poll_once()
    for name, wert in (hebel or {}).items():
        driver.write(name, wert)
    return driver


def _kern_folge(erzeuger, hebel, ziel, aussen_je_schritt):
    zustand = hausmodell.Anlagenzustand(room=20.0, burner_hours=1000.0)
    for aussen in aussen_je_schritt:
        hausmodell.anlage_schritt(
            zustand, hausmodell.HOUSES["H1"], hausmodell.Potenzgesetz(*erzeuger), hebel, aussen, 0.0, 12.0, ziel,
            hausmodell.Betriebsflags(liefert=True), None)
    return zustand


@pytest.mark.parametrize("versatz", [0.0, 3 * 3600.0, 86400.0 + 5 * 3600.0])
def test_the_daily_course_of_the_outdoor_temperature_moves_with_the_offset(paths, paths_zwei, clock, versatz):
    echt = [10 * 3600.0]
    ohne = _sim_zeit(paths, clock, echt)
    verschoben = _sim_zeit(paths_zwei, clock, [echt[0] + versatz])
    assert ohne.read_signal("outdoor_temp") == round(_tagesgang(echt[0]), 2)
    assert verschoben.read_signal("outdoor_temp") == round(_tagesgang(echt[0] + versatz), 2)


def test_without_an_injected_clock_the_driver_follows_the_functional_time(paths, clock, monkeypatch):
    echte = [10 * 3600.0]
    monkeypatch.setattr(zeitquelle, "echte_epoche", lambda: echte[0])
    monkeypatch.setattr(zeitquelle, "_VERSATZ_S", 6 * 3600.0)
    driver = create("simulation", {"lever_set": "vaillant_vrc720", "aussen_amplitude": AMPLITUDE, "aussen_mittel": MITTEL},
                    paths, clock=clock, writer=True, description=BINDINGS["vaillant_vrc720"])
    driver.poll_once()
    assert driver.read_signal("outdoor_temp") == round(_tagesgang(16 * 3600.0), 2)
    assert driver.read_signal("outdoor_temp") != round(_tagesgang(10 * 3600.0), 2)


def test_every_step_of_a_poll_uses_the_outdoor_temperature_of_its_own_time(paths, paths_zwei, clock):
    geschrieben, erzeuger, hebel, ziel = ABBILDUNG["vaillant_vrc720"]
    start = 6 * 3600.0
    walls = [start]
    einmal = _sim_zeit(paths, clock, walls, hebel=geschrieben)
    clock.advance(96 * SCHRITT)
    walls[0] = start + 96 * SCHRITT
    einmal.poll_once()
    erwartet = _kern_folge(erzeuger, hebel, ziel, [_tagesgang(start + k * SCHRITT) for k in range(1, 97)])
    assert einmal.read_signal("room_temperature") == round(erwartet.room, 2)
    assert einmal.read_signal("generator_hours") == round(erwartet.burner_hours, 3)


def test_polling_in_between_gives_the_same_room_as_one_long_poll(paths, paths_zwei, clock):
    geschrieben = ABBILDUNG["vaillant_vrc720"][0]
    start = 6 * 3600.0
    walls_a, walls_b = [start], [start]
    uhr_b = type(clock)()
    oft = _sim_zeit(paths, clock, walls_a, hebel=geschrieben)
    einmal = _sim_zeit(paths_zwei, uhr_b, walls_b, hebel=geschrieben)
    for k in range(1, 25):
        clock.advance(SCHRITT)
        walls_a[0] = start + k * SCHRITT
        oft.poll_once()
    uhr_b.advance(24 * SCHRITT)
    walls_b[0] = start + 24 * SCHRITT
    einmal.poll_once()
    assert oft.read_signal("room_temperature") == einmal.read_signal("room_temperature")
    assert oft.read_signal("generator_hours") == einmal.read_signal("generator_hours")


def test_the_remainder_keeps_the_time_of_the_steps_of_the_daily_course(paths, clock):
    geschrieben, erzeuger, hebel, ziel = ABBILDUNG["vaillant_vrc720"]
    start = 6 * 3600.0
    walls = [start]
    driver = _sim_zeit(paths, clock, walls, hebel=geschrieben)
    for gesamt in range(700, 35001, 700):
        clock.advance(700)
        walls[0] = start + gesamt
        driver.poll_once()
        erwartet = _kern_folge(erzeuger, hebel, ziel, [_tagesgang(start + k * SCHRITT) for k in range(1, gesamt // 300 + 1)])
        assert driver.read_signal("room_temperature") == round(erwartet.room, 2), gesamt


def test_a_jump_of_the_wall_clock_does_not_change_the_room(paths, paths_zwei, clock):
    geschrieben, erzeuger, hebel, ziel = ABBILDUNG["vaillant_vrc720"]
    walls = [WALL]
    sprung = _sim_zeit(paths, clock, walls, hebel=geschrieben, aussen_amplitude=0.0)
    for delta in (86400.0, -5 * 3600.0, 40 * 86400.0):
        walls[0] += delta
        sprung.poll_once()
    assert sprung.read_signal("room_temperature") == 20.0
    clock.advance(12 * SCHRITT)
    walls[0] -= 30 * 86400.0
    sprung.poll_once()
    erwartet = _kern_folge(erzeuger, hebel, ziel, [MITTEL] * 12)
    assert sprung.read_signal("room_temperature") == round(erwartet.room, 2)
    assert sprung.read_signal("generator_hours") == round(erwartet.burner_hours, 3)


def test_the_wall_clock_alone_runs_no_step(paths, clock):
    walls = [WALL]
    driver = _sim_zeit(paths, clock, walls, hebel=ABBILDUNG["vaillant_vrc720"][0])
    walls[0] += 3 * 86400.0
    driver.poll_once()
    assert driver.read_signal("room_temperature") == 20.0
    assert driver.read_signal("generator_hours") == 1000.0


@pytest.mark.parametrize("heizkreis", [0, 1, 7])
def test_the_plant_files_keep_their_form_and_keys(paths, clock, heizkreis):
    walls = [1234.0]
    driver = _sim_zeit(paths, clock, walls, heizkreis=heizkreis, settle_delay=60)
    driver.write("curve", 1.3)
    datei = paths.sim_dir / ("plant.json" if heizkreis == 0 else f"plant-{heizkreis}.json")
    plant = json.loads(datei.read_text())
    assert sorted(plant) == ["geschrieben_um", "hebel", "hilfswerte", "vorbereitet", "vorher"]
    assert plant["hebel"]["curve"] == 1.3
    assert plant["geschrieben_um"] == {"curve": 1234.0}


def test_every_heating_circuit_has_its_own_house(paths, clock):
    erzeuger, hebel_a, hebel_b = ABBILDUNG["vaillant_vrc720"][1], (0.5, 18.0, 16.0), (2.0, 24.0, 16.0)
    walls = [WALL]
    kreis0 = _sim_zeit(paths, clock, walls, aussen_amplitude=0.0, heizkreis=0)
    kreis1 = _sim_zeit(paths, clock, walls, aussen_amplitude=0.0, heizkreis=1)
    kreis0.write("curve", 0.5), kreis0.write("room_setpoint", 18.0), kreis0.write("min_flow", 18.0)
    kreis1.write("curve", 2.0), kreis1.write("room_setpoint", 24.0), kreis1.write("min_flow", 24.0)
    clock.advance(36 * SCHRITT)
    kreis0.poll_once()
    kreis1.poll_once()
    for kreis, hebel in ((kreis0, hebel_a), (kreis1, hebel_b)):
        erwartet = _kern_folge(erzeuger, hebel, hebel[1], [MITTEL] * 36)
        assert kreis.read_signal("room_temperature") == round(erwartet.room, 2)
        assert kreis.read_signal("generator_hours") == round(erwartet.burner_hours, 3)
    assert kreis0.read_signal("room_temperature") != kreis1.read_signal("room_temperature")
