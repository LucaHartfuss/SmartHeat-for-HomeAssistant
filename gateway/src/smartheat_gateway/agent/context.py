"""Gemeinsamer Zustand der Agent-Befehle (Spec SHG G2 6.3). runtime_status und raum spiegelt der Agent vom lokalen Bus
(retained); device_state setzt die Agent-Schleife aus der Server-Antwort."""
import time
from collections.abc import Callable

from smartheat_gateway import topics
from smartheat_gateway.agent.identity import Identity
from smartheat_gateway.bus import Bus, decode
from smartheat_gateway.paths import Paths
from smartheat_gateway.zigbee import ZigbeeMirror


class AgentContext:
    def __init__(
        self, paths: Paths, bus: Bus, mirror: ZigbeeMirror, identity: Identity, *,
        clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
    ) -> None:
        self.paths, self.bus, self.mirror, self.identity = paths, bus, mirror, identity
        self.clock, self.wall = clock, wall
        self.device_state: str | None = None
        self.runtime_status: dict | None = None
        self.raum: dict | None = None
        self.register_requested = False
        self.diagnostics: Callable[[], dict] = dict

    def start(self) -> None:
        self.bus.subscribe(topics.STATUS, self._on_status)
        self.bus.subscribe(topics.RAUM, self._on_raum)

    def _on_status(self, topic: str, raw: bytes, retain: bool) -> None:
        body = decode(raw)
        self.runtime_status = body if isinstance(body, dict) else None

    def _on_raum(self, topic: str, raw: bytes, retain: bool) -> None:
        body = decode(raw)
        self.raum = body if isinstance(body, dict) else None
