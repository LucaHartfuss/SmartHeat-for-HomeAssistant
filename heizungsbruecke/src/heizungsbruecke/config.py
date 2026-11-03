"""Feste Pfade des Add-ons. Die Einstellungen kommen aus dem Konfigurationsdokument des Geraetekerns
(dokument.bindung); options.json liest nur __main__ (bootstrap_url, log_level)."""
from pathlib import Path

from smartheat_core import zustandspfade

# Dateien im Add-on-Datenverzeichnis. Nutzer lesen sie zur Laufzeit als `config.<NAME>`, damit
# Tests sie umbiegen koennen.
DATA_DIR = Path("/data")
OPTIONS_PATH = DATA_DIR / "options.json"
DERIVED_SENSORS_PATH = DATA_DIR / "derived_sensors.json"
# Sicherung im Format vor der Kreisordnung: die Laufzeit liest sie nie, ihr Vorhandensein ohne Kreisordner stoppt den
# Start, bis die Zustandsmigration gelaufen ist.
ALTER_BACKUP_PATH = DATA_DIR / "backup.json"
# Laufzeit-Zustand (Soll-Speicher, Abmelde-Markierung) wie im Gateway unter <daten>/runtime/; den Geraetekern legt
# selbst device/ unter DEVICE_DATA_DIR an.
RUNTIME_DIR = DATA_DIR / "runtime"
ABGEMELDET_PATH = RUNTIME_DIR / "abgemeldet.json"
AUFRAEUMEN_PATH = DATA_DIR / "aufraeumen.json"
DEVICE_DATA_DIR = DATA_DIR
# Rastung des Sicherheitssatzes: gleich smartheat_device.device.DEVICE_DIR / smartheat_core.rastung.DATEI
# (ein Test haelt das gleich); die Laufzeit liest sie, der Dokumentspeicher des Geraetekerns schreibt sie.
RASTUNG_PATH = DEVICE_DATA_DIR / "device" / "sicherheitssatz.json"


# Zustand je Heizkreis unter RUNTIME_DIR/kreis-<n>/; RUNTIME_DIR wird zur Laufzeit gelesen, damit Tests es umbiegen koennen.
def kreis_dir(kreis: int) -> Path:
    return zustandspfade.kreis_dir(RUNTIME_DIR, kreis)


def backup_path(kreis: int) -> Path:
    return kreis_dir(kreis) / zustandspfade.BACKUP


def failsafe_path(kreis: int) -> Path:
    return kreis_dir(kreis) / zustandspfade.FAILSAFE


def entitlement_path(kreis: int) -> Path:
    return kreis_dir(kreis) / zustandspfade.ENTITLEMENT


def room_target_path(kreis: int) -> Path:
    return kreis_dir(kreis) / zustandspfade.ROOM_TARGET


def anlage_budget_path() -> Path:
    return RUNTIME_DIR / zustandspfade.ANLAGE_BUDGET
