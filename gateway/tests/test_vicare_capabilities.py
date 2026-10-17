"""Feature-Lesehilfen des Treibers und die Kuerzung fuer die Erkennung (Spec G4 3, Stufe 3 S5b). Die Klassifikation in
Kandidaten und die Matrix der Aufzeichnungen liegen auf dem Server (tests/test_gateway_erkennung.py)."""
import json

import pytest

from smartheat_gateway.drivers.vicare_cloud import capabilities as caps


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


# --- Erkennung (Stufe 3 S5b): gekuerzte Features und Hebelstand fuer die Inventur ---


def test_gekuerzt_keeps_only_name_properties_and_command_params_and_drops_uris():
    roh = {"feature": "heating.circuits.0.heating.curve", "apiVersion": 1, "deviceId": "0", "gatewayId": "G1",
           "uri": "https://x/installations/123", "isEnabled": True, "components": [],
           "properties": {"slope": number(1.4)},
           "commands": {"setCurve": {"isExecutable": True, "name": "setCurve", "uri": "https://x/installations/123",
                                     "params": {"slope": constraint(0.2, 3.5, 0.1)}}}}
    assert caps.gekuerzt(roh) == {
        "feature": "heating.circuits.0.heating.curve", "properties": {"slope": number(1.4)},
        "commands": {"setCurve": {"isExecutable": True, "params": {"slope": constraint(0.2, 3.5, 0.1)}}}}


def test_gekuerzt_drops_features_outside_the_prefix_list_and_garbage():
    for name in ("heating.dhw.temperature", "device.messages.errors.raw", "heating.circuits", "x"):
        assert caps.gekuerzt(feature(name, {"value": number(1)})) is None
    for garbage in (None, 5, "heating.circuits.0", {}, {"feature": 7}, {"feature": None}):
        assert caps.gekuerzt(garbage) is None
    assert caps.gekuerzt({"feature": "heating.burners.0", "properties": "x", "commands": [1]}) == {
        "feature": "heating.burners.0", "properties": {}, "commands": {}}
    assert caps.gekuerzt({"feature": "heating.boiler.sensors.temperature.main",
                          "commands": {"c": "x", 5: {"isExecutable": True}}})["commands"] == {
        "c": {"isExecutable": None, "params": {}}}


def test_gekuerzt_drops_personal_data_of_the_customer():
    """G-9: freier Text (Kreisname), Seriennummer, Zeitprogramme (Anwesenheitsmuster) und Urlaubsdaten gehen nie hoch."""
    zeitprogramm = {"type": "Schedule", "value": {"mon": [{"start": "06:00", "end": "22:00", "mode": "normal"}]}}
    assert caps.gekuerzt(feature("heating.circuits.0", {"active": {"type": "boolean", "value": True},
                                                        "name": {"type": "string", "value": "Leonies Zimmer"}})
                         )["properties"] == {"active": {"type": "boolean", "value": True}}
    assert caps.gekuerzt(feature("heating.boiler.serial", {"value": {"type": "string", "value": "7637415"}})) is None
    assert caps.gekuerzt(feature("heating.circuits.0.operating.programs.holiday", {
        "active": {"type": "boolean", "value": False}, "start": {"type": "string", "value": "2026-12-24"},
        "end": {"type": "string", "value": "2027-01-02"}}))["properties"] == {"active": {"type": "boolean", "value": False}}
    assert caps.gekuerzt(feature("heating.circuits.0.heating.schedule", {"entries": zeitprogramm}))["properties"] == {}


def test_gekuerzt_einer_ganzen_anlage_hat_nichts_persoenliches_und_keine_uri():
    zeitprogramm = {"type": "Schedule", "value": {"mon": [{"start": "06:00", "end": "22:00", "mode": "normal"}]}}
    roh = good_features(circuits=(0, 1)) + [
        feature("heating.circuits.0", {"name": {"type": "string", "value": "Leonies Zimmer"}, "active": {"value": True}}),
        feature("heating.circuits.0.heating.schedule", {"entries": zeitprogramm}),
        feature("heating.circuits.0.operating.programs.holiday", {"start": {"value": "2026-12-24"}, "end": {"value": "x"}}),
        feature("heating.boiler.serial", {"value": {"type": "string", "value": "7637415"}}),
        feature("heating.dhw.temperature", {"value": number(50)}),
        feature("device.messages.errors.raw", {"entries": {"type": "array", "value": []}}),
    ]
    for eintrag in roh:
        eintrag["uri"] = "https://api.viessmann.example/iot/v2/features/installations/123"
        eintrag["commands"] = {key: {**cmd, "uri": "https://x/installations/123", "name": key}
                               for key, cmd in eintrag["commands"].items()}
    kurz = [k for k in map(caps.gekuerzt, roh) if k is not None]
    assert len(kurz) == len(roh) - 3  # Seriennummer, Warmwasser und Meldungen liegen ausserhalb der Positivliste
    for eintrag in kurz:
        assert set(eintrag) == {"feature", "properties", "commands"}
        assert not set(caps.PERSOENLICH) & set(eintrag["properties"]), eintrag["feature"]
        assert all(prop.get("type") != "Schedule" for prop in eintrag["properties"].values())
        assert all(set(cmd) == {"isExecutable", "params"} for cmd in eintrag["commands"].values())
        assert "http" not in json.dumps(eintrag), eintrag["feature"]
    assert not any(k["feature"].endswith(".serial") for k in kurz)


def test_hebelstand_is_none_when_a_lever_is_missing():
    index = caps.by_name(good_features())
    assert set(caps.hebelstand(index, 0)) == {"curve", "level", "room_setpoint"}
    assert caps.hebelstand(index, 1) is None  # kein Heizkreis 1
    assert caps.hebelstand(caps.by_name(good_features(with_normal=False)), 0) is None
    assert caps.hebelstand(caps.by_name(good_features(with_curve_command=False)), 0) is None


GARBAGE = [None, 5, "x", [], {"feature": 3}, {"feature": "heating.circuits.0.heating.curve", "properties": "x", "commands": []},
           {"feature": "heating.circuits.0.heating.curve", "properties": {"slope": 5, "shift": None}, "commands": {"setCurve": 7}},
           {"feature": "heating.circuits.0.operating.programs.normal", "properties": {"temperature": {"value": "warm"}},
            "commands": {"setTemperature": {"isExecutable": True, "params": {"targetTemperature": {"constraints": []}}}}}]


def test_garbage_features_do_not_raise_in_gekuerzt_and_hebelstand():
    for roh in GARBAGE:
        caps.gekuerzt(roh)
    index = caps.by_name(GARBAGE)
    assert caps.circuit_numbers(index) == [0]
    assert caps.hebelstand(index, 0) is None and caps.lever_ranges(index, 0) == {
        "curve": None, "level": None, "room_setpoint": None}
    assert caps.generator_signals(index) == {} and caps.mode_signal(index, 0) is None


@pytest.mark.parametrize("huge", [10**400, -10**400], ids=["gross-positiv", "gross-negativ"])
def test_integer_beyond_float_range_is_no_number_like_any_non_numeric_value(huge):
    # JSON liefert beliebig grosse Ganzzahlen; float(10**400) wirft OverflowError und brach den ganzen Abruf ab
    assert caps.number_value(feature("f", {"value": number(huge)}), "value") is None
    index = caps.by_name([
        feature("heating.burners.0.statistics", {"hours": number(huge), "starts": number(98765)}),
        feature("heating.compressors.0.statistics", {"hours": number(huge)}),
    ])
    assert caps.generator_signals(index) == {"generator_starts": 98765.0}
    roh = feature("heating.circuits.0.heating.curve", {}, {"setCurve": {
        "isExecutable": True, "params": {"slope": constraint(0.2, huge, 0.1)}}})
    assert caps.command_range(roh, "setCurve", "slope") is None


def test_command_range_braucht_ein_ausfuehrbares_kommando_und_alle_drei_grenzen():
    # Abschlussreview S5b (Mutationsprobe): isExecutable False allein genuegt zur Ablehnung, ebenso jede einzelne fehlende
    # Grenze; ohne isExecutable und mit allen Grenzen gilt das Kommando.
    def mit(**kommando):
        return feature("heating.circuits.0.heating.curve", {}, {"setCurve": {"params": {"slope": constraint(0.2, 3.5, 0.1)}, **kommando}})

    assert caps.command_range(mit(isExecutable=True), "setCurve", "slope") == (0.2, 3.5, 0.1)
    assert caps.command_range(mit(), "setCurve", "slope") == (0.2, 3.5, 0.1)
    assert caps.command_range(mit(isExecutable=False), "setCurve", "slope") is None
    for fehlt in ("min", "max", "stepping"):
        constraints = {"min": 0.2, "max": 3.5, "stepping": 0.1}
        del constraints[fehlt]
        roh = feature("heating.circuits.0.heating.curve", {}, {"setCurve": {
            "isExecutable": True, "params": {"slope": {"constraints": constraints}}}})
        assert caps.command_range(roh, "setCurve", "slope") is None, fehlt
    index = caps.by_name(good_features())
    assert caps.lever_ranges(index, 0)["curve"] == (0.2, 3.5, 0.1)
