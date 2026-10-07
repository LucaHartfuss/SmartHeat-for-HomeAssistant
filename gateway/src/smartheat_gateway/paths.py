"""Dateien unter /data (Spec SHG G2 6.1, Plan G2a Praezisierung 3). Ein Schreiber je Datei: Agent = runtime_config,
secrets/, device/, agent/, agent_state.json; Laufzeit = runtime/; quota/ unter flock."""
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
    quota_dir = property(lambda self: self.data / "quota")
    sim_dir = property(lambda self: self.data / "sim")


def from_env() -> Paths:
    return Paths(Path(os.environ.get("SHG_DATA_DIR", "/data")))
