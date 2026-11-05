"""Die Anlagen der Akzeptanzsuite: vier Hebelsaetze, je Heizkoerper und Fussbodenheizung. Alles Literale (Entities,
Startwerte, Bindungsparameter wie der Server sie sendet); nichts kommt aus dem Produktcode, damit ein Fehler dort nicht
auch die Erwartung verschiebt."""
import copy

VERTEILSYSTEME = ("Heizkoerper", "Fussbodenheizung")
SAETZE = ("vaillant_vrc720", "weishaupt_wwp", "weishaupt_wwp_basis", "viessmann_vicare")
ALLE = [(satz, verteilsystem) for verteilsystem in VERTEILSYSTEME for satz in SAETZE]

#: Hebel des Hebelsatzes, die ein Szenario gezielt setzt; den Mindestvorlauf (Vaillant) sendet der Server zusaetzlich,
#: siehe `serverhebel_von`
_HEBEL = {
    "vaillant_vrc720": ("curve", "room_setpoint", "heat_limit"),
    "weishaupt_wwp": ("curve", "room_setpoint", "heat_limit"),
    "weishaupt_wwp_basis": ("room_setpoint",),
    "viessmann_vicare": ("curve", "level", "room_setpoint"),
}
PLATTFORM = {"vaillant_vrc720": "mypyllant", "weishaupt_wwp": "weishaupt_modbus",
             "weishaupt_wwp_basis": "weishaupt_modbus", "viessmann_vicare": "vicare"}
KONFIG_EINTRAG = {"mypyllant": "e-mypyllant", "weishaupt_modbus": "e-weishaupt", "vicare": "e-vicare"}

_WEISHAUPT_ROLLEN = {"room_setpoint": "number.normal", "mode_select": "select.betriebsart",
                     "setpoint_comfort": "number.komfort", "setpoint_setback": "number.absenk"}
#: Rolle -> Entity der Bindung (room_target und outdoor_temp sind Sensoren, alle anderen Schreibziele)
_ROLLEN = {
    "vaillant_vrc720": {"curve": "number.curve_current", "room_setpoint": "number.shift_current",
                        "heat_limit": "number.heat_limit", "min_flow": "number.min_flow"},
    "weishaupt_wwp": {"curve": "number.hk", "heat_limit": "number.swu", **_WEISHAUPT_ROLLEN},
    "weishaupt_wwp_basis": dict(_WEISHAUPT_ROLLEN),
    "viessmann_vicare": {"curve": "number.slope", "level": "number.shift",
                         "room_setpoint": "number.normal_temperature", "mode_select": "climate.heizkreis"},
}
#: Das Thermostat des Bewohners: eine Climate-Entity, der Wunsch steht im Attribut `temperature` (wie bei `client1`)
THERMOSTAT = "climate.thermostat"
_SENSOREN = {"room_target": f"{THERMOSTAT}::temperature", "outdoor_temp": "sensor.outdoor_temp"}
RAUMFUEHLER = "sensor.room_actual"

#: Startzustand der Schreibziele (innerhalb der Grenzen, nirgends gleich einem Minimum); je Verteilsystem
_START = {
    ("vaillant_vrc720", "Heizkoerper"): {"number.curve_current": 0.9, "number.shift_current": 21.0,
                                         "number.heat_limit": 16.0, "number.min_flow": 21.0},
    ("vaillant_vrc720", "Fussbodenheizung"): {"number.curve_current": 0.25, "number.shift_current": 21.0,
                                              "number.heat_limit": 15.0, "number.min_flow": 21.0},
    ("weishaupt_wwp", "Heizkoerper"): {"number.hk": 0.75, "number.swu": 18.0},
    ("weishaupt_wwp", "Fussbodenheizung"): {"number.hk": 0.3, "number.swu": 15.0},
    ("weishaupt_wwp_basis", "Heizkoerper"): {},
    ("weishaupt_wwp_basis", "Fussbodenheizung"): {},
    ("viessmann_vicare", "Heizkoerper"): {"number.slope": 1.0, "number.shift": 0.0,
                                          "number.normal_temperature": 20.0},
    ("viessmann_vicare", "Fussbodenheizung"): {"number.slope": 0.3, "number.shift": 0.0,
                                               "number.normal_temperature": 20.0},
}
_WEISHAUPT_START = {"number.normal": 21.0, "number.komfort": 22.0, "number.absenk": 18.0,
                    "select.betriebsart": "hz_operationmode_normal"}
_VIESSMANN_START = {"climate.heizkreis": "heat", "climate.heizkreis::preset_mode": "home"}

_KANAELE = ("electrical_heating", "electrical_dhw", "primary_heating", "primary_dhw", "thermal_heating",
            "thermal_dhw", "electrical_total")
_SCHUTZ_FREI = {"tag": None, "lebensdauer_hinweis": None, "durchsetzen_tag": 6}
_SCHUTZ_EEPROM = {"tag": 10, "lebensdauer_hinweis": 50000, "durchsetzen_tag": 6}
_SCHEMATA = {"praefixe": ["hz_operationmode_", "heating_circuit_operation_mode_"],
             "normal_bereich": {"hz_operationmode_": [16.0, 28.0], "heating_circuit_operation_mode_": [18.0, 25.0]}}
_PARAMETER = {
    "vaillant_vrc720": {
        "schritte": {"curve": 0.05, "room_setpoint": 0.5, "heat_limit": 0.1, "min_flow": 0.1},
        "toleranzen": {"curve": 0.025, "room_setpoint": 0.25, "heat_limit": 0.05, "min_flow": 0.05},
        "wartezeit_s": 2100, "schreibgruppen": [], "zurueck": ["curve", "room_setpoint", "heat_limit", "min_flow"],
        "zurueck_optional": ["heat_limit"],
        "nur_lesen": [], "zaehler": "daily", "schreibschutz": _SCHUTZ_FREI, "schemata": {},
    },
    "weishaupt_wwp": {
        "schritte": {"curve": 0.05, "room_setpoint": 0.5, "heat_limit": 0.5},
        "toleranzen": {"curve": 0.025, "room_setpoint": 0.25, "heat_limit": 0.25},
        "wartezeit_s": 120, "schreibgruppen": [], "zurueck": ["curve", "room_setpoint", "heat_limit"],
        "zurueck_optional": [], "nur_lesen": [], "zaehler": "daily", "schreibschutz": _SCHUTZ_EEPROM,
        "schemata": _SCHEMATA,
    },
    "weishaupt_wwp_basis": {
        "schritte": {"room_setpoint": 0.5}, "toleranzen": {"room_setpoint": 0.25}, "wartezeit_s": 120,
        "schreibgruppen": [], "zurueck": ["room_setpoint"], "zurueck_optional": [], "nur_lesen": ["curve"],
        "zaehler": "daily", "schreibschutz": _SCHUTZ_EEPROM, "schemata": _SCHEMATA,
    },
    "viessmann_vicare": {
        "schritte": {"curve": 0.1, "level": 1.0, "room_setpoint": 1.0},
        "toleranzen": {"curve": 0.05, "level": 0.5, "room_setpoint": 0.5}, "wartezeit_s": 180,
        "schreibgruppen": [["curve", "level"]], "zurueck": ["curve", "level", "room_setpoint"],
        "zurueck_optional": [], "nur_lesen": [], "zaehler": "daily",
        "schreibschutz": {"tag": None, "lebensdauer_hinweis": None, "durchsetzen_tag": 4}, "schemata": {},
    },
}


def hebel_von(satz: str) -> tuple[str, ...]:
    return _HEBEL[satz]


def serverhebel_von(satz: str) -> tuple[str, ...]:
    """Alle Hebel, die der Server in einer Antwort sendet: die des Satzes, bei Vaillant dazu der Mindestvorlauf."""
    return (*_HEBEL[satz], *(("min_flow",) if satz == "vaillant_vrc720" else ()))


def schreibziel(satz: str, hebel: str) -> str:
    return _ROLLEN[satz][hebel]


def zustand(satz: str, verteilsystem: str) -> dict:
    """Zustand der Fake-HA beim Start: Schreibziele, Sensoren (mit device_class im Zustand wie im echten HA)."""
    zustand = {"sensor.room_actual": 20.0, "sensor.outdoor_temp": 5.0, THERMOSTAT: "heat",
               f"{THERMOSTAT}::temperature": 21.0, **_START[(satz, verteilsystem)]}
    if satz.startswith("weishaupt"):
        zustand.update(_WEISHAUPT_START)
    if satz == "viessmann_vicare":
        zustand.update(_VIESSMANN_START)
    zustand.update({f"sensor.{name}::device_class": "temperature"
                    for name in ("room_actual", "outdoor_temp")})
    return zustand


def schreibziele(satz: str) -> list[dict]:
    """Registry-Eintraege: die Schreibziele (eine Plattform, ein Config-Entry, ein Geraet: der Waermeerzeuger) und das
    Thermostat (eigene Plattform, eigenes Geraet)."""
    plattform = PLATTFORM[satz]
    return [{"entity_id": entity, "platform": plattform, "config_entry_id": KONFIG_EINTRAG[plattform],
             "device_id": f"d-{plattform}"} for entity in _ROLLEN[satz].values()] + [
        {"entity_id": THERMOSTAT, "platform": "generic_thermostat", "config_entry_id": "e-thermostat",
         "device_id": "d-thermostat"}]


#: Anbindungsweg je Plattform der Bindung
_WEG = {"mypyllant": "vaillant_myvaillant", "vicare": "viessmann_vicare", "weishaupt_modbus": "weishaupt_modbus"}


def konfiguration(satz: str, verteilsystem: str, abo: str = "aktiv") -> dict:
    """Konfigurationsdokument (ohne schema/version) wie der Server es sendet; `abo` ist das des einen Kreises."""
    plattform = PLATTFORM[satz]
    parameter = copy.deepcopy(_PARAMETER[satz])
    parameter["energie"] = {kanal: "kWh" for kanal in _KANAELE}
    bindung = {"kreis": {"plattform": plattform, "config_entry": KONFIG_EINTRAG[plattform], "anlage": "S",
                         "nummer": "0"},
               "rollen": {**_ROLLEN[satz], **_SENSOREN}, "batterien": [], "benachrichtigung": [],
               "hinweise_aus": []}
    raum = {"raum": 1, "name": "Wohnzimmer", "rolle": "referenz", "fuehler": [RAUMFUEHLER], "soll_quelle": False,
            "versatz_k": 0.0, "ziel_fest": None}
    return {
        "anlage": "akzeptanz", "anlage_name": "Akzeptanzanlage", "weg": _WEG[plattform],
        "kreise": [{"kreis": 1, "name": "Kreis 1", "bindung": bindung, "raeume": [raum], "hebelsatz": satz,
                    "verteilsystem": verteilsystem, "abo": abo}],
        "tagestick": "12:00", "mindest_software": "1.0.0", "mindest_integration": None,
        "software": None, "bindungsparameter": parameter,
    }


#: Konfiguration ohne Anlage: das Abmelden vom Dienst (Felder wie die Konfiguration, ohne Inhalt)
LEERE_KONFIGURATION = {"anlage": None, "anlage_name": None, "weg": None, "kreise": [], "tagestick": None,
                       "mindest_software": "1.0.0", "mindest_integration": None, "software": None,
                       "bindungsparameter": None}
