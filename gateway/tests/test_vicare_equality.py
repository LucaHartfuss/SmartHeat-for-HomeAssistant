"""Gleichheitstest (Spec G4 3): dasselbe Szenario ueber ViessmannHaBinding (Fake-HA) und vicare_cloud (Fake-ViCare)
ergibt dieselben Hebelentscheidungen, Schreibzaehlungen und zurueckgestellten Ursprungswerte."""
import importlib.util
from pathlib import Path

import pytest
from fake_vicare.server import GOOD_CODE, FakeVicare
from heizungsbruecke.ha_binding import ViessmannHaBinding

from smartheat_core.binding import VIESSMANN_VICARE_BINDING as BINDING
from smartheat_core.pipeline import LeverPipeline
from smartheat_core.safety import resolve_local_safety
from smartheat_gateway.drivers.vicare_cloud.driver import ViCareCloudDriver
from smartheat_gateway.paths import Paths
from smartheat_runtime.roles import ChannelManifest

# FakeHa kommt aus dem HA-Host-Test des Add-ons (eine Quelle, kein Duplikat). Per Dateipfad geladen statt ueber den
# Suchpfad: heizungsbruecke/tests und gateway/tests haben gleichnamige Module (conftest, fakes, test_config, ...).
_HA_TEST = Path(__file__).resolve().parents[2] / "heizungsbruecke" / "tests" / "test_ha_binding_viessmann.py"
_spec = importlib.util.spec_from_file_location("ha_binding_viessmann_tests", _HA_TEST)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
FakeHa = _module.FakeHa

MANIFEST = ChannelManifest(refs={
    "curve_current": "number.slope", "level_current": "number.shift", "shift_current": "number.normal_temperature",
    "mode_select": "climate.heizkreis",
})
SAFETY = resolve_local_safety("viessmann_vicare", "Heizkoerper")
NAMES = {"number.slope": "curve", "number.shift": "level", "number.normal_temperature": "room_setpoint"}


def curve_pairs(log):
    """Anzahl der Schreibgruppen (curve unmittelbar gefolgt von level) in einem normalisierten Protokoll."""
    return sum(1 for first, second in zip(log, log[1:], strict=False)
               if first[:2] == ("hebel", "curve") and second[:2] == ("hebel", "level"))


class HaWorld:
    """Fake-HA wie in test_ha_binding_viessmann; normalisiert jeden Schreibvorgang auf (Art, Hebel/Programm, Wert)."""

    def __init__(self, program):
        self.ha = FakeHa(program, **{"number.slope": 1.4, "number.shift": 0.0, "number.normal_temperature": 20.0})
        self.binding = ViessmannHaBinding(self.ha, MANIFEST, BINDING)

    def calls(self, log):
        """Physische Aufrufe: der HA-Pfad schreibt zwei number-Entities je Schreibgruppe, der Treiber nur ein setCurve
        (Audit 4, A4-31); gleichgesetzt wird die Gruppe als EIN Aufruf (so zaehlt sie auch die Pipeline)."""
        return self.binding.physical_writes - curve_pairs(log)

    def log(self):
        return [("preset", call[2]) if call[0] == "preset" else ("hebel", NAMES[call[1]], call[2])
                for call in self.ha.calls]

    def clear(self):
        self.ha.calls.clear()

    def refresh(self):
        pass


class CloudWorld:
    def __init__(self, program, tmp_path, clock, monkeypatch):
        self.server = FakeVicare()
        base = self.server.start()
        for key, value in {"SHG_TEST_ENDPOINTS": "1", "SHG_VICARE_IAM_BASE": base, "SHG_VICARE_API_BASE": base}.items():
            monkeypatch.setenv(key, value)
        self.driver = ViCareCloudDriver(
            {"installation_id": 2012345, "gateway_serial": "7637415000000001", "device_id": "0", "heizkreis": 0},
            Paths(tmp_path), clock=clock, writer=True)
        self.driver.login_begin({"client_id": "c", "redirect_uri": "r"})
        self.driver.login_finish({"code": GOOD_CODE, "redirect_uri": "r"})
        for feature in self.server.features["0"]:
            if feature["feature"].endswith("programs.active"):
                feature["properties"]["value"]["value"] = program
            if feature["feature"].endswith(f"programs.{program}") and "active" in feature["properties"]:
                # "normal" hat keine active-Eigenschaft (Basisprogramm): dort genuegt programs.active
                feature["properties"]["active"]["value"] = True
        self.driver.poll_once()
        self.curve_writes = []
        self._record_curve_writes()
        self.binding = self.driver

    def _record_curve_writes(self):
        """Haelt fest, fuer welchen Hebel der Treiber write() aufruft (oder dass write_group beide schreibt): setCurve
        traegt immer BEIDE Werte und gibt deshalb nicht preis, welcher Hebel gemeint war (der HA-Pfad schreibt zwei
        number-Entities einzeln)."""
        real_write = self.driver.write

        def write(lever, value):
            real_write(lever, value)
            if lever in ("curve", "level"):
                self.curve_writes.append(lever)

        self.driver.write = write
        real_group = self.driver.write_group

        def write_group(values):
            real_group(values)
            self.curve_writes.append("gruppe")  # Audit 4, A4-31: ein setCurve fuer beide Mitglieder

        self.driver.write_group = write_group

    def calls(self, log):
        return self.binding.physical_writes

    def log(self):
        entries, writes = [], list(self.curve_writes)
        for command in self.server.commands:
            if command["command"] == "setCurve":
                # genau ein setCurve je Hebelschreibvorgang, in der Reihenfolge der Aufrufe
                lever = writes.pop(0)
                if lever == "gruppe":
                    # Gruppenschreibung (A4-31): der HA-Pfad schreibt dieselben zwei Hebel nacheinander (curve, level)
                    entries.append(("hebel", "curve", command["params"]["slope"]))
                    entries.append(("hebel", "level", float(command["params"]["shift"])))
                    continue
                value = command["params"]["slope"] if lever == "curve" else float(command["params"]["shift"])
                entries.append(("hebel", lever, value))
            elif command["command"] == "setTemperature":
                entries.append(("hebel", "room_setpoint", command["params"]["targetTemperature"]))
            elif command["command"] == "deactivate":
                entries.append(("preset", "home"))
            elif command["command"] == "activate":
                entries.append(("preset", command["feature"].rsplit(".", 1)[-1]))
        assert not writes, f"Hebelschreibvorgaenge ohne setCurve-Befehl: {writes}"
        return entries

    def clear(self):
        self.server.commands.clear()
        self.curve_writes.clear()

    def refresh(self):
        self.driver.poll_once()


def scenario(world, store, clock):
    """Start mit Vorbereitung (aus Eco bzw. Normalprogramm), Tagestick mit neuer Kurve, Abmelden mit Zuruecksetzen.

    Nicht abgedeckt (Backlog): das Durchsetzen nach einem Eingriff von aussen (es gibt hier keinen) und der
    Komfort-Boost aus Spec G4 3."""
    pipeline = LeverPipeline(store, world.binding, SAFETY, clock=clock)
    clock.advance(BINDING.settle_seconds + 1)
    pipeline.apply_server_values({"curve": 1.1, "level": 2.0, "room_setpoint": 21.0})
    start_log = world.log()
    start_writes = world.calls(start_log)
    originals, aux = dict(store.state.originals), dict(store.state.aux_originals)
    world.clear()
    world.refresh()
    clock.advance(BINDING.settle_seconds + 1)
    pipeline.apply_server_values({"curve": 1.0, "level": 2.0, "room_setpoint": 21.0})
    tick_log = world.log()
    world.clear()
    assert pipeline.restore_and_clear(always_restore=False) is True
    return {"start": start_log, "start_writes": start_writes, "originals": originals, "aux": aux, "tick": tick_log,
            "restore": world.log()}


@pytest.mark.parametrize("program", ["eco", "normal"])
def test_ha_path_and_gateway_driver_make_the_same_decisions(program, make_store, clock, tmp_path, monkeypatch):
    ha_program = "home" if program == "normal" else program
    # Jede Welt hat ihren eigenen Zustandsspeicher: ein gemeinsames backup.json wuerde die Ursprungswerte des ersten
    # Laufs in den zweiten tragen, und der zweite haette seine eigenen nie erfasst.
    ha_store = make_store(directory=tmp_path / "ha-state")
    cloud_store = make_store(directory=tmp_path / "cloud-state")
    assert ha_store.state.originals == cloud_store.state.originals == {}
    ha_result = scenario(HaWorld(ha_program), ha_store, clock)
    assert cloud_store.state.originals == {}, "der Lauf des HA-Pfads darf den Speicher des Cloud-Laufs nicht beruehren"
    cloud = CloudWorld(program, tmp_path / "cloud", clock, monkeypatch)
    try:
        cloud_result = scenario(cloud, cloud_store, clock)
    finally:
        cloud.server.stop()
    ha_result["aux"] = {k: ("normal" if v == "home" else v) for k, v in ha_result["aux"].items()}
    assert cloud_result == ha_result
