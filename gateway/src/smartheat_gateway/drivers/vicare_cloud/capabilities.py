"""Features einer Viessmann-Anlage lesen (Bereiche und Schrittweiten aus den Kommando-Constraints) und fuer die
Erkennung auf dem Server kuerzen. Rein, ohne I/O, ohne Modellverzweigung: alles kommt aus den
Features; die Klassifikation in Kandidaten macht der Server (einrichtung/gateway_erkennung.py). Features haben von der
Cloud keinen garantierten Typ, alles wird defensiv gelesen."""
import re

from smartheat_core.zahlen import ist_endliche_zahl

LEVER_SET = "viessmann_vicare"
#: Features, die die Erkennung hochlaedt (Positivliste, G-9): alles andere (Warmwasser, Meldungen, Geraeteangaben) bleibt
#: auf dem Gateway.
PRAEFIXE = ("heating.circuits.", "heating.sensors.", "heating.burners.", "heating.compressors.", "heating.boiler.")
#: Eigenschaften mit Personenbezug, die nie hochgehen (G-9): Kreisname (freier Text des Kunden) und die Daten des
#: Urlaubsprogramms; dazu jede Eigenschaft vom Typ Schedule (Zeitprogramm = Anwesenheitsmuster) und jedes Feature
#: `*.serial` (Seriennummer des Geraets, z. B. heating.boiler.serial).
PERSOENLICH = ("name", "start", "end")
CURVE = "heating.circuits.{n}.heating.curve"
NORMAL = "heating.circuits.{n}.operating.programs.normal"
ACTIVE = "heating.circuits.{n}.operating.programs.active"
OUTSIDE = "heating.sensors.temperature.outside"
# Rolle -> (Feature, Eigenschaft); {n} = Heizkreis. Namen aus der Matrix belegt (test_the_signal_feature_names_...).
SIGNAL_FEATURES: dict[str, tuple[str, str]] = {
    "outdoor_temp": (OUTSIDE, "value"),
    "flow_temperature": ("heating.circuits.{n}.sensors.temperature.supply", "value"),
    "room_temperature": ("heating.circuits.{n}.sensors.temperature.room", "value"),
}
# Liefer-Signale: Rolle -> Kandidaten (Feature, Eigenschaft), der erste mit Zahl gilt. Brenner 0
# (Gastherme) vor Verdichter 0 (Waermepumpe); aus PyViCare abgeleitet, an einer echten Anlage erst im G4-Pilot geprueft
# (ungeprueft).
GENERATOR_FEATURES: dict[str, tuple[tuple[str, str], ...]] = {
    "generator_hours": (("heating.burners.0.statistics", "hours"), ("heating.compressors.0.statistics", "hours")),
    "generator_starts": (("heating.burners.0.statistics", "starts"), ("heating.compressors.0.statistics", "starts")),
}
GENERATOR_ACTIVE = ("heating.burners.0", "heating.compressors.0")  # Eigenschaft active (bool) -> "on"/"off"
MODE = "heating.circuits.{n}.operating.modes.active"  # Eigenschaft value: z. B. dhwAndHeating, dhw, standby (ungeprueft)
OPTIONAL_SIGNALS = tuple(SIGNAL_FEATURES) + ("generator_hours", "generator_starts", "generator_state", "operating_mode")
_CIRCUIT = re.compile(r"^heating\.circuits\.(\d+)\.")


def by_name(features) -> dict[str, dict]:
    return {f["feature"]: f for f in features if isinstance(f, dict) and isinstance(f.get("feature"), str)}


def circuit_numbers(index: dict[str, dict]) -> list[int]:
    return sorted({int(m.group(1)) for name in index if (m := _CIRCUIT.match(name))})


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def value(feature: dict | None, prop: str):
    return _dict(_dict(_dict(feature).get("properties")).get(prop)).get("value")


def _number(raw) -> float | None:
    return float(raw) if ist_endliche_zahl(raw) else None


def number_value(feature: dict | None, prop: str) -> float | None:
    return _number(value(feature, prop))


def generator_signals(index: dict[str, dict]) -> dict[str, float | str]:
    found: dict[str, float | str] = {}
    for role, candidates in GENERATOR_FEATURES.items():
        for name, prop in candidates:
            number = number_value(index.get(name), prop)
            if number is not None:
                found[role] = number
                break
    for name in GENERATOR_ACTIVE:
        active = value(index.get(name), "active")
        if isinstance(active, bool):
            found["generator_state"] = "on" if active else "off"
            break
    return found


def mode_signal(index: dict[str, dict], n: int) -> str | None:
    mode = value(index.get(MODE.format(n=n)), "value")
    return mode if isinstance(mode, str) and mode else None


def command_range(feature: dict | None, command: str, param: str) -> tuple[float, float, float] | None:
    """(min, max, stepping) des Parameters eines ausfuehrbaren Kommandos, sonst None."""
    cmd = _dict(_dict(_dict(feature).get("commands")).get(command))
    if not cmd or cmd.get("isExecutable") is False:
        return None
    constraints = _dict(_dict(_dict(cmd.get("params")).get(param)).get("constraints"))
    low, high, step = (_number(constraints.get(key)) for key in ("min", "max", "stepping"))
    if low is None or high is None or step is None:
        return None
    return low, high, step


def lever_ranges(index: dict[str, dict], n: int) -> dict[str, tuple[float, float, float] | None]:
    """(min, max, stepping) je Hebel aus den Kommando-Constraints des Heizkreises n; gemeinsam fuer die Sicherheitswarnungen
    der Erkennung (`hebelstand`) und den Wertebereich des Treibers (PlantBinding.limits)."""
    curve, normal = index.get(CURVE.format(n=n)), index.get(NORMAL.format(n=n))
    return {"curve": command_range(curve, "setCurve", "slope"), "level": command_range(curve, "setCurve", "shift"),
            "room_setpoint": command_range(normal, "setTemperature", "targetTemperature")}


def gekuerzt(feature: dict) -> dict | None:
    """Feature fuer die Inventur (G-9): nur Name, Eigenschaften und Kommandos (ausfuehrbar, Parameter), ohne URIs,
    Geraete- und Gateway-Kennungen und ohne persoenliche Eigenschaften; None ausserhalb der Positivliste."""
    name = feature.get("feature") if isinstance(feature, dict) else None
    if not isinstance(name, str) or not name.startswith(PRAEFIXE) or name.endswith(".serial"):
        return None
    commands = _dict(feature.get("commands"))
    properties = {key: prop for key, prop in _dict(feature.get("properties")).items()
                  if key not in PERSOENLICH and _dict(prop).get("type") != "Schedule"}
    return {"feature": name, "properties": properties,
            "commands": {key: {"isExecutable": _dict(cmd).get("isExecutable"), "params": _dict(_dict(cmd).get("params"))}
                         for key, cmd in commands.items() if isinstance(key, str)}}


def hebelstand(index: dict[str, dict], n: int) -> dict[str, dict] | None:
    """Aktueller Wert und Anlagenbereich der drei Hebel des Heizkreises n (fuer die Sicherheitswarnungen im Geraet,
    G-1); None, wenn einer fehlt."""
    curve, normal = index.get(CURVE.format(n=n)), index.get(NORMAL.format(n=n))
    now = {"curve": number_value(curve, "slope"), "level": number_value(curve, "shift"),
           "room_setpoint": number_value(normal, "temperature")}
    stand = {}
    for lever, bereich in lever_ranges(index, n).items():
        if bereich is None or now[lever] is None:
            return None
        stand[lever] = {"wert": now[lever], "min": bereich[0], "max": bereich[1]}
    return stand
