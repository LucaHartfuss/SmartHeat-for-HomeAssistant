"""Feature-Liste einer Viessmann-Anlage -> Probe-Kandidaten (Spec SHG G4 1.3). Rein, ohne I/O, ohne Modellverzweigung:
alles kommt aus den Features (Bereiche und Schrittweiten aus den Kommando-Constraints). Features haben von der Cloud
keinen garantierten Typ, alles wird defensiv gelesen."""
import re

from smartheat_core.binding import VIESSMANN_VICARE_BINDING as BINDING
from smartheat_runtime.roles import LEVER_ROLES

LEVER_SET = "viessmann_vicare"
REJECTIONS = ("heizkurve_nicht_schreibbar", "kein_normalprogramm", "schrittweite_abweichend", "erzeuger_unbekannt",
              "aussentemperatur_fehlt")
REJECTION_TEXTS = {
    "heizkurve_nicht_schreibbar":
        "Die Heizkurve dieses Heizkreises lässt sich über die Viessmann-Schnittstelle nicht ändern.",
    "kein_normalprogramm": "Dieser Heizkreis hat kein einstellbares Normalprogramm (Wunschtemperatur).",
    "schrittweite_abweichend":
        "Die Einstellschritte dieser Anlage weichen von den erwarteten ab; sie wird noch nicht unterstützt.",
    "erzeuger_unbekannt": "Der Wärmeerzeuger dieser Anlage (Hybrid oder unbekannt) wird noch nicht unterstützt.",
    "aussentemperatur_fehlt": "Die Anlage meldet keine Außentemperatur.",
}
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
    return float(raw) if isinstance(raw, int | float) and not isinstance(raw, bool) else None


def number_value(feature: dict | None, prop: str) -> float | None:
    return _number(value(feature, prop))


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


def generator(index: dict[str, dict]) -> str | None:
    burner = any(name.startswith("heating.burners.") for name in index)
    compressor = any(name.startswith("heating.compressors.") for name in index)
    if burner == compressor:  # weder noch (unbekannt) oder beides (Hybrid)
        return None
    return "gastherme" if burner else "waermepumpe"


def _levers(index: dict[str, dict], n: int) -> tuple[dict, str | None]:
    """(hebel, ablehnungsgrund). Reihenfolge der Pruefungen = Reihenfolge von REJECTIONS."""
    curve, normal = index.get(CURVE.format(n=n)), index.get(NORMAL.format(n=n))
    slope = command_range(curve, "setCurve", "slope")
    shift = command_range(curve, "setCurve", "shift")
    if slope is None or shift is None:
        return {}, "heizkurve_nicht_schreibbar"
    setpoint = command_range(normal, "setTemperature", "targetTemperature")
    if setpoint is None:
        return {}, "kein_normalprogramm"
    now = {"curve": number_value(curve, "slope"), "level": number_value(curve, "shift"),
           "room_setpoint": number_value(normal, "temperature")}
    ranges = {"curve": slope, "level": shift, "room_setpoint": setpoint}
    if any(abs(ranges[lever][2] - BINDING.steps[lever]) > 1e-9 for lever in ranges):
        return {}, "schrittweite_abweichend"
    if any(v is None for v in now.values()):
        return {}, "heizkurve_nicht_schreibbar"
    return {lever: {"wert": now[lever], "min": ranges[lever][0], "max": ranges[lever][1], "schritt": ranges[lever][2]}
            for lever in ("curve", "level", "room_setpoint")}, None


def signals_for(index: dict[str, dict], n: int) -> list[str]:
    roles = [LEVER_ROLES[lever] for lever in ("curve", "level", "room_setpoint")] + ["mode_select"]
    for role, (template, prop) in SIGNAL_FEATURES.items():
        if number_value(index.get(template.format(n=n)), prop) is not None:
            roles.append(role)
    return sorted(roles)


def candidates(features, *, installation_id: int, gateway_serial: str, device_id: str,
               poll_seconds: float) -> list[dict]:
    index = by_name(features)
    kind = generator(index)
    result = []
    for n in circuit_numbers(index):
        levers, reason = _levers(index, n)
        if reason is None and kind is None:
            reason = "erzeuger_unbekannt"
        if reason is None and number_value(index.get(OUTSIDE), "value") is None:
            reason = "aussentemperatur_fehlt"
        result.append({
            "kandidat_id": f"vicare-{installation_id}-{gateway_serial}-{device_id}-hk{n}",
            "anzeige": f"Viessmann · Heizkreis {n}",
            "parameter": {"installation_id": installation_id, "gateway_serial": gateway_serial,
                          "device_id": device_id, "heizkreis": n, "poll_seconds": poll_seconds,
                          "lever_set": LEVER_SET},
            "erzeuger_typ": kind or "unbekannt", "lever_set": None if reason else LEVER_SET,
            "ablehnung": {"grund": reason, "text": REJECTION_TEXTS[reason]} if reason else None,
            "hebel": {} if reason else levers, "signale": [] if reason else signals_for(index, n),
            "sicherheitswarnungen": {}, "details": {"modell_hinweis": None},
        })
    return result
