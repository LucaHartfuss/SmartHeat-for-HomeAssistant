"""Dateien unter /data (Spec SHG G2 6.1, Spec 5b Plan D2). Ein Schreiber je Datei: Geraetekern = device/ (Identitaet,
Zertifikat, Dokumente, Befehle); Laufzeit = runtime/, agent_state.json (LED, Name aus 0.5.0), inventur.json,
aufraeumen.json, software_soll.json, software_quittung.json; Host-Updater = software_meldungen.json; quota/ unter
flock. (runtime_config, secrets/ und agent/ gehoeren zum Agenten bis 0.5.0 und entfallen mit Task 8.)"""
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    data: Path

    runtime_config = property(lambda self: self.data / "runtime_config.json")
    secrets_dir = property(lambda self: self.data / "secrets")
    runtime_secrets = property(lambda self: self.data / "secrets" / "runtime.json")
    transport_key = property(lambda self: self.data / "secrets" / "transport_key.pem")
    driver_secrets_dir = property(lambda self: self.data / "secrets" / "drivers")
    device_dir = property(lambda self: self.data / "device")
    agent_dir = property(lambda self: self.data / "agent")
    inventory_samples = property(lambda self: self.data / "agent" / "inventory.json")
    agent_state = property(lambda self: self.data / "agent_state.json")
    runtime_dir = property(lambda self: self.data / "runtime")
    backup = property(lambda self: self.data / "runtime" / "backup.json")
    failsafe = property(lambda self: self.data / "runtime" / "failsafe_state.json")
    entitlement = property(lambda self: self.data / "runtime" / "entitlement_state.json")
    room_target = property(lambda self: self.data / "runtime" / "room_target.json")
    inventur = property(lambda self: self.data / "inventur.json")
    abgemeldet = property(lambda self: self.data / "runtime" / "abgemeldet.json")
    aufraeumen = property(lambda self: self.data / "aufraeumen.json")
    software_soll = property(lambda self: self.data / "software_soll.json")
    software_meldungen = property(lambda self: self.data / "software_meldungen.json")
    software_quittung = property(lambda self: self.data / "software_quittung.json")
    quota_dir = property(lambda self: self.data / "quota")
    sim_dir = property(lambda self: self.data / "sim")


def from_env() -> Paths:
    return Paths(Path(os.environ.get("SHG_DATA_DIR", "/data")))
