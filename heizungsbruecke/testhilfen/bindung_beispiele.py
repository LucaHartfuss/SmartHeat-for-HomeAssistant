"""Testhilfe (Spec Stufe 3 S4b): die Bindungsparameter, die der Server heute je Hebelsatz sendet, und die daraus
gebauten Beschreibungen unter den frueheren Namen aus smartheat_core.binding. Nur fuer Tests -- das Geraet hat keine
Ersatztabelle. Dieselben Werte haelt der Server-Test tests/test_bindungen.py; ein Contract-Check (Vertrag V4) baut sie
als Rundweg."""
import copy

from smartheat_core.bindungsparameter import ENERGIE_KANAELE, beschreibung

_SCHUTZ_FREI = {"tag": None, "lebensdauer_hinweis": None, "durchsetzen_tag": 6}
_SCHUTZ_EEPROM = {"tag": 10, "lebensdauer_hinweis": 50000, "durchsetzen_tag": 6}
WEISHAUPT_SCHEMATA = {"praefixe": ["hz_operationmode_", "heating_circuit_operation_mode_"],
                      "normal_bereich": {"hz_operationmode_": [16.0, 28.0],
                                         "heating_circuit_operation_mode_": [18.0, 25.0]}}
#: Tests lesen Energie in kWh (Attribut unit_of_measurement in den HA-Attrappen); Wh-Faelle setzen es selbst.
ALLE_KWH = {kanal: "kWh" for kanal in ENERGIE_KANAELE}

PARAMETER: dict[str, dict] = {
    "vaillant_vrc720": {
        "schritte": {"curve": 0.05, "room_setpoint": 0.5, "heat_limit": 0.1, "min_flow": 0.1},
        "toleranzen": {"curve": 0.025, "room_setpoint": 0.25, "heat_limit": 0.05, "min_flow": 0.05},
        "wartezeit_s": 2100, "schreibgruppen": [], "zurueck": ["heat_limit"], "zurueck_optional": ["heat_limit"],
        "nur_lesen": [], "zaehler": "daily", "schreibschutz": _SCHUTZ_FREI, "schemata": {}, "energie": ALLE_KWH,
    },
    "weishaupt_wwp": {
        "schritte": {"curve": 0.05, "room_setpoint": 0.5, "heat_limit": 0.5},
        "toleranzen": {"curve": 0.025, "room_setpoint": 0.25, "heat_limit": 0.25},
        "wartezeit_s": 120, "schreibgruppen": [], "zurueck": ["curve", "room_setpoint", "heat_limit"],
        "zurueck_optional": [], "nur_lesen": [], "zaehler": "daily", "schreibschutz": _SCHUTZ_EEPROM,
        "schemata": WEISHAUPT_SCHEMATA, "energie": ALLE_KWH,
    },
    "weishaupt_wwp_basis": {
        "schritte": {"room_setpoint": 0.5}, "toleranzen": {"room_setpoint": 0.25}, "wartezeit_s": 120,
        "schreibgruppen": [], "zurueck": ["room_setpoint"], "zurueck_optional": [], "nur_lesen": ["curve"],
        "zaehler": "daily", "schreibschutz": _SCHUTZ_EEPROM, "schemata": WEISHAUPT_SCHEMATA, "energie": ALLE_KWH,
    },
    "viessmann_vicare": {
        "schritte": {"curve": 0.1, "level": 1.0, "room_setpoint": 1.0},
        "toleranzen": {"curve": 0.05, "level": 0.5, "room_setpoint": 0.5}, "wartezeit_s": 180,
        "schreibgruppen": [["curve", "level"]], "zurueck": ["curve", "level", "room_setpoint"],
        "zurueck_optional": [], "nur_lesen": [], "zaehler": "daily",
        "schreibschutz": {"tag": None, "lebensdauer_hinweis": None, "durchsetzen_tag": 4}, "schemata": {},
        "energie": ALLE_KWH,
    },
}


def parameter(hebelsatz: str, **aenderungen) -> dict:
    """Tiefe Kopie der Parameter eines Hebelsatzes mit geaenderten Feldern (fuer Pruef-Tests)."""
    return {**copy.deepcopy(PARAMETER[hebelsatz]), **aenderungen}


BINDINGS = {hebelsatz: beschreibung(hebelsatz, werte) for hebelsatz, werte in PARAMETER.items()}
VAILLANT_MYPYLLANT = BINDINGS["vaillant_vrc720"]
WEISHAUPT_MODBUS = BINDINGS["weishaupt_wwp"]
WEISHAUPT_MODBUS_BASIS = BINDINGS["weishaupt_wwp_basis"]
VIESSMANN_VICARE_BINDING = BINDINGS["viessmann_vicare"]
