"""Feste Pfade des Add-ons. Die Einstellungen kommen aus dem Konfigurationsdokument des Geraetekerns
(dokument.bindung); options.json liest nur __main__ (bootstrap_url, log_level)."""
from pathlib import Path

# Dateien im Add-on-Datenverzeichnis. Nutzer lesen sie zur Laufzeit als `config.<NAME>`, damit
# Tests sie umbiegen koennen.
DATA_DIR = Path("/data")
OPTIONS_PATH = DATA_DIR / "options.json"
BACKUP_PATH = DATA_DIR / "backup.json"
FAILSAFE_PATH = DATA_DIR / "failsafe_state.json"
DERIVED_SENSORS_PATH = DATA_DIR / "derived_sensors.json"
ENTITLEMENT_PATH = DATA_DIR / "entitlement_state.json"
# Laufzeit-Zustand (Soll-Speicher, Abmelde-Markierung) wie im Gateway unter <daten>/runtime/; den Geraetekern legt
# selbst device/ unter DEVICE_DATA_DIR an.
RUNTIME_DIR = DATA_DIR / "runtime"
ROOM_TARGET_PATH = RUNTIME_DIR / "room_target.json"
ABGEMELDET_PATH = RUNTIME_DIR / "abgemeldet.json"
AUFRAEUMEN_PATH = DATA_DIR / "aufraeumen.json"
DEVICE_DATA_DIR = DATA_DIR
# Rastung des Sicherheitssatzes (Spec Stufe 3 S3 3.3): gleich smartheat_device.device.DEVICE_DIR / smartheat_core.rastung.DATEI
# (ein Test haelt das gleich); die Laufzeit liest sie, der Dokumentspeicher des Geraetekerns schreibt sie.
RASTUNG_PATH = DEVICE_DATA_DIR / "device" / "sicherheitssatz.json"
