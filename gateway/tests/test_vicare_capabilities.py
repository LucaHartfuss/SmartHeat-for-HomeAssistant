"""capabilities.candidates gegen synthetische Faelle und die Matrix (Spec G4 3)."""
import json
from pathlib import Path

import pytest

from smartheat_core.binding import VIESSMANN_VICARE_BINDING as BINDING
from smartheat_gateway.agent import wire
from smartheat_gateway.drivers.vicare_cloud import capabilities as caps

MATRIX = Path(__file__).parent / "vicare_matrix"
IDS = {"installation_id": 1, "gateway_serial": "G1", "device_id": "0", "poll_seconds": 300.0}


def feature(name, properties=None, commands=None):
    return {"feature": name, "isEnabled": True, "properties": properties or {}, "commands": commands or {}}


def number(value):
    return {"type": "number", "value": value}


def constraint(low, high, step):
    return {"type": "number", "required": True, "constraints": {"min": low, "max": high, "stepping": step}}


def good_features(*, slope_step=0.1, with_normal=True, with_curve_command=True, burner=True, compressor=False,
                  outside=True, circuits=(0,)):
    features = []
    for n in circuits:
        curve_commands = {"setCurve": {"isExecutable": True, "params": {
            "shift": constraint(-13, 40, 1), "slope": constraint(0.2, 3.5, slope_step)}}} if with_curve_command else {}
        features.append(feature(f"heating.circuits.{n}.heating.curve", {"shift": number(0), "slope": number(1.4)},
                                curve_commands))
        if with_normal:
            features.append(feature(f"heating.circuits.{n}.operating.programs.normal", {"temperature": number(20)}, {
                "setTemperature": {"isExecutable": True, "params": {"targetTemperature": constraint(3, 37, 1)}}}))
        features.append(feature(f"heating.circuits.{n}.operating.programs.active",
                                {"value": {"type": "string", "value": "normal"}}))
        features.append(feature(f"heating.circuits.{n}.sensors.temperature.supply", {"value": number(35)}))
    if outside:
        features.append(feature("heating.sensors.temperature.outside", {"value": number(5)}))
    if burner:
        features.append(feature("heating.burners.0"))
    if compressor:
        features.append(feature("heating.compressors.0"))
    return features


def only(features, **overrides):
    result = caps.candidates(features, **{**IDS, **overrides})
    assert len(result) == 1
    return result[0]


def test_a_gas_boiler_with_a_writable_curve_gives_a_full_candidate():
    candidate = only(good_features())
    assert tuple(candidate) == wire.PROBE_CANDIDATE_FIELDS
    assert candidate["ablehnung"] is None and candidate["lever_set"] == "viessmann_vicare"
    assert candidate["erzeuger_typ"] == "gastherme"
    assert candidate["hebel"] == {
        "curve": {"wert": 1.4, "min": 0.2, "max": 3.5, "schritt": 0.1},
        "level": {"wert": 0, "min": -13, "max": 40, "schritt": 1},
        "room_setpoint": {"wert": 20, "min": 3, "max": 37, "schritt": 1},
    }
    assert candidate["parameter"] == {"installation_id": 1, "gateway_serial": "G1", "device_id": "0", "heizkreis": 0,
                                      "poll_seconds": 300.0, "lever_set": "viessmann_vicare"}
    assert {"outdoor_temp", "curve_current", "level_current", "shift_current", "mode_select"} <= set(candidate["signale"])
    assert candidate["sicherheitswarnungen"] == {}


@pytest.mark.parametrize(("kwargs", "reason"), [
    ({"with_curve_command": False}, "heizkurve_nicht_schreibbar"),
    ({"with_normal": False}, "kein_normalprogramm"),
    ({"slope_step": 0.5}, "schrittweite_abweichend"),
    ({"burner": False}, "erzeuger_unbekannt"),
    ({"burner": True, "compressor": True}, "erzeuger_unbekannt"),
    ({"outside": False}, "aussentemperatur_fehlt"),
])
def test_each_rejection_has_its_reason_and_a_customer_text(kwargs, reason):
    candidate = only(good_features(**kwargs))
    assert candidate["lever_set"] is None and candidate["hebel"] == {} and candidate["signale"] == []
    assert candidate["ablehnung"] == {"grund": reason, "text": caps.REJECTION_TEXTS[reason]}


def test_every_reason_has_a_text_and_the_driver_list_matches():
    assert set(caps.REJECTION_TEXTS) == set(caps.REJECTIONS)


def test_a_non_writable_second_circuit_is_rejected_while_the_first_stays_valid():
    features = good_features(circuits=(0,)) + [
        feature("heating.circuits.1.heating.curve", {"shift": number(0), "slope": number(1)})]
    first, second = caps.candidates(features, **IDS)
    assert first["ablehnung"] is None and second["ablehnung"]["grund"] == "heizkurve_nicht_schreibbar"
    assert (first["kandidat_id"], second["kandidat_id"]) == ("vicare-1-G1-0-hk0", "vicare-1-G1-0-hk1")


def test_a_device_without_any_heating_circuit_gives_no_candidate():
    assert caps.candidates([feature("heating.burners.0")], **IDS) == []


def test_garbage_features_do_not_raise():
    garbage = [None, 5, {"feature": 3},
               {"feature": "heating.circuits.0.heating.curve", "properties": "x", "commands": []}]
    assert all(c["ablehnung"] for c in caps.candidates(garbage, **IDS))


def test_the_steps_come_from_the_binding_not_a_second_table():
    candidate = only(good_features())
    assert {k: v["schritt"] for k, v in candidate["hebel"].items()} == dict(BINDING.steps)


EXPECTED = json.loads((MATRIX / "expected.json").read_text())
RECORDINGS = sorted(p for p in (MATRIX / "recordings").glob("*.json"))


def test_every_recording_has_an_expectation_and_vice_versa():
    assert {p.name for p in RECORDINGS} == set(EXPECTED), "neue Aufzeichnung ohne Erwartung (oder umgekehrt)"


@pytest.mark.parametrize("path", RECORDINGS, ids=lambda p: p.name)
def test_matrix(path):
    features = json.loads(path.read_text()).get("data", [])
    result = caps.candidates(features, **IDS)
    actual = [{"heizkreis": c["parameter"]["heizkreis"], "lever_set": c["lever_set"],
               "grund": (c["ablehnung"] or {}).get("grund"), "erzeuger_typ": c["erzeuger_typ"]} for c in result]
    assert actual == EXPECTED[path.name]


def test_the_signal_feature_names_exist_in_at_least_one_recording():
    seen = set()
    for path in RECORDINGS:
        seen |= {f.get("feature") for f in json.loads(path.read_text()).get("data", [])}
    for role, (template, _prop) in caps.SIGNAL_FEATURES.items():
        assert template.format(n=0) in seen, f"{role}: {template} kommt in keiner Aufzeichnung vor"


def test_generator_signals_from_the_burner_features():
    # Audit 4 P-B (W-1): Liefer-Signale aus den Brenner-Features
    index = caps.by_name([
        {"feature": "heating.burners.0", "properties": {"active": {"type": "boolean", "value": True}}},
        {"feature": "heating.burners.0.statistics",
         "properties": {"hours": {"type": "number", "value": 4321.0}, "starts": {"type": "number", "value": 98765}}},
        {"feature": "heating.circuits.0.operating.modes.active", "properties": {"value": {"type": "string", "value": "dhw"}}},
    ])
    assert caps.generator_signals(index) == {"generator_hours": 4321.0, "generator_starts": 98765.0, "generator_state": "on"}
    assert caps.mode_signal(index, 0) == "dhw"


def test_generator_signals_fall_back_to_the_compressor():
    index = caps.by_name([{"feature": "heating.compressors.0.statistics",
                           "properties": {"hours": {"type": "number", "value": 12.5}}}])
    assert caps.generator_signals(index) == {"generator_hours": 12.5}


def test_generator_signals_ignore_garbage_and_missing_features():
    index = caps.by_name([
        {"feature": "heating.burners.0", "properties": {"active": {"type": "string", "value": "yes"}}},
        {"feature": "heating.burners.0.statistics", "properties": {"hours": {"type": "number", "value": "x"}}},
        {"feature": "heating.circuits.0.operating.modes.active", "properties": {"value": {"type": "string", "value": ""}}},
    ])
    assert caps.generator_signals(index) == {} and caps.mode_signal(index, 0) is None
    assert caps.generator_signals({}) == {} and caps.mode_signal({}, 0) is None


def test_the_probe_lists_the_delivery_roles_that_are_present():
    features = good_features() + [
        feature("heating.burners.0.statistics", {"hours": number(10), "starts": number(3)}),
        feature("heating.circuits.0.operating.modes.active", {"value": {"type": "string", "value": "heating"}})]
    signals = only(features)["signale"]
    assert {"generator_hours", "generator_starts", "operating_mode"} <= set(signals)
    assert "generator_state" not in signals  # kein Eigenschaft active am Brenner
    assert not {"generator_hours", "generator_starts", "operating_mode"} & set(only(good_features())["signale"])
