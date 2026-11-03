"""Dateien unter /data. Ein Schreiber je Datei: Geraetekern = device/ (Identitaet,
Zertifikat, Dokumente, Befehle); Laufzeit = runtime/, agent_state.json (LED), inventur.json,
aufraeumen.json, werksreset_bereit.json, werksreset_abmeldung.json, software_soll.json, software_quittung.json, secrets/drivers/ (Treiber-Anmeldungen); Host-Updater =
software_meldungen.json; quota/ unter einem Lock je Pfad im Prozess."""
import os
from dataclasses import dataclass
from pathlib import Path

# Die Namen stehen hier woertlich, weil der Host dieses Modul ohne den Geraetekern installiert (nur `dateien.py` daneben);
# ein Test haelt sie gleich `smartheat_core.zustandspfade`.
_ANLAGE_BUDGET = "anlage_budget.json"
_BACKUP = "backup.json"
_FAILSAFE = "failsafe_state.json"
_ENTITLEMENT = "entitlement_state.json"
_ROOM_TARGET = "room_target.json"


@dataclass(frozen=True)
class Paths:
    data: Path

    driver_secrets_dir = property(lambda self: self.data / "secrets" / "drivers")
    device_dir = property(lambda self: self.data / "device")
    rastung = property(lambda self: self.data / "device" / "sicherheitssatz.json")  # = device_dir / rastung.DATEI
    agent_state = property(lambda self: self.data / "agent_state.json")
    runtime_dir = property(lambda self: self.data / "runtime")
    inventur = property(lambda self: self.data / "inventur.json")
    #: Sicherung im Format vor der Kreisordnung; die Laufzeit liest sie nie, nur ihr Vorhandensein stoppt den Start
    alter_backup = property(lambda self: self.data / "runtime" / "backup.json")
    anlage_budget = property(lambda self: self.runtime_dir / _ANLAGE_BUDGET)
    abgemeldet = property(lambda self: self.data / "runtime" / "abgemeldet.json")
    werksreset_bereit = property(lambda self: self.data / "werksreset_bereit.json")
    #: Vermerk des Geraete-Hosts: diese Abmeldung (abgemeldet.json) stammt von einer Reset-Anfrage {seq, ts, anlage}
    werksreset_abmeldung = property(lambda self: self.data / "werksreset_abmeldung.json")
    aufraeumen = property(lambda self: self.data / "aufraeumen.json")
    software_soll = property(lambda self: self.data / "software_soll.json")
    software_meldungen = property(lambda self: self.data / "software_meldungen.json")
    software_quittung = property(lambda self: self.data / "software_quittung.json")
    quota_dir = property(lambda self: self.data / "quota")
    sim_dir = property(lambda self: self.data / "sim")

    def kreis_dir(self, kreis: int) -> Path:
        """Zustandsordner eines Heizkreises: runtime/kreis-<n>/."""
        if isinstance(kreis, bool) or not isinstance(kreis, int) or not 1 <= kreis <= 9999:
            raise ValueError("Kreisnummer ausserhalb des Bereichs")
        return self.runtime_dir / f"kreis-{kreis}"

    def backup(self, kreis: int) -> Path:
        return self.kreis_dir(kreis) / _BACKUP

    def failsafe(self, kreis: int) -> Path:
        return self.kreis_dir(kreis) / _FAILSAFE

    def entitlement(self, kreis: int) -> Path:
        return self.kreis_dir(kreis) / _ENTITLEMENT

    def room_target(self, kreis: int) -> Path:
        return self.kreis_dir(kreis) / _ROOM_TARGET


def from_env() -> Paths:
    return Paths(Path(os.environ.get("SHG_DATA_DIR", "/data")))
