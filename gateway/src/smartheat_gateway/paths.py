"""Dateien unter /data. Ein Schreiber je Datei: Geraetekern = device/ (Identitaet,
Zertifikat, Dokumente, Befehle); Laufzeit = runtime/, agent_state.json (LED), inventur.json,
aufraeumen.json, werksreset_bereit.json, software_soll.json, software_quittung.json, secrets/drivers/ (Treiber-Anmeldungen); Host-Updater =
software_meldungen.json; quota/ unter einem Lock je Pfad im Prozess."""
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    data: Path

    driver_secrets_dir = property(lambda self: self.data / "secrets" / "drivers")
    device_dir = property(lambda self: self.data / "device")
    rastung = property(lambda self: self.data / "device" / "sicherheitssatz.json")  # = device_dir / rastung.DATEI
    agent_state = property(lambda self: self.data / "agent_state.json")
    runtime_dir = property(lambda self: self.data / "runtime")
    backup = property(lambda self: self.data / "runtime" / "backup.json")
    failsafe = property(lambda self: self.data / "runtime" / "failsafe_state.json")
    entitlement = property(lambda self: self.data / "runtime" / "entitlement_state.json")
    room_target = property(lambda self: self.data / "runtime" / "room_target.json")
    inventur = property(lambda self: self.data / "inventur.json")
    abgemeldet = property(lambda self: self.data / "runtime" / "abgemeldet.json")
    werksreset_bereit = property(lambda self: self.data / "werksreset_bereit.json")
    aufraeumen = property(lambda self: self.data / "aufraeumen.json")
    software_soll = property(lambda self: self.data / "software_soll.json")
    software_meldungen = property(lambda self: self.data / "software_meldungen.json")
    software_quittung = property(lambda self: self.data / "software_quittung.json")
    quota_dir = property(lambda self: self.data / "quota")
    sim_dir = property(lambda self: self.data / "sim")


def from_env() -> Paths:
    return Paths(Path(os.environ.get("SHG_DATA_DIR", "/data")))
