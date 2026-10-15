"""Konfigurations- und Bedienungsdokumente fuer Tests (Testwerte, keine Zugangsdaten)."""
from smartheat_core import dateien

SENSOR = "0x00124b0000000001"
THERMOSTAT = "0x00124b0000000002"


def konfiguration(**changes) -> dict:
    """Konfigurationsdokument (Inhalt ohne schema/version) fuer den Simulations-Treiber; Testwerte."""
    value = {
        "anlage": "test-tenant", "profil": "viessmann_gastherme_heizkoerper", "hebelsatz": "viessmann_vicare",
        "verteilsystem": "Heizkoerper",
        "bindung": {"treiber": {"id": "simulation", "parameter": {"lever_set": "viessmann_vicare", "heizkreis": 0}},
                    "thermostat": THERMOSTAT},
        "raumfuehler": [f"zigbee:{SENSOR}:temperature"], "tagestick": "12:00", "abo": "aktiv",
        "mindest_software": "0.6.0", "software": None, "bindungsparameter": None,
    }
    value.update(changes)
    return value


def leer() -> dict:
    """Leere Konfiguration (Spec 5b 3.1, wie device_config.leer() des Servers): kein Geraet eingerichtet."""
    return {**konfiguration(), "anlage": None, "profil": None, "hebelsatz": None, "verteilsystem": None,
            "bindung": None, "tagestick": None, "abo": None, "raumfuehler": []}


def bedienung(raum_soll: float = 20.0) -> dict:
    return {"modus": "manuell", "raum_soll": raum_soll, "quelle": "portal", "ts": "2026-01-15T07:00:00+00:00"}


def dokumente_schreiben(paths, konfiguration=None, bedienung=None, *, version: int = 1) -> None:
    """Dokumente direkt im Format des Dokumentspeichers ablegen (wie nach einer Uebernahme, D1 Task 3)."""
    for name, inhalt in (("konfiguration.json", konfiguration), ("bedienung.json", bedienung)):
        if inhalt is not None:
            dateien.schreiben_json(paths.device_dir / name, {"version": version, "inhalt": inhalt, "dirty": False},
                                   modus=dateien.PRIVAT)
