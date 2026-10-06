"""Einstieg des Agenten (Spec SHG G2 6): Identitaet, lokaler Bus, Zigbee-Spiegel, Zigbee2MQTT-Grundkonfiguration,
Diagnoseseite, Geraete-API, Schleife. Umgebung: Plan G2a Praezisierung 12, dazu SHG_DIAG_HOSTNAMES (kommagetrennte
Namen, unter denen die Diagnoseseite neben IP-Adressen und localhost antworten darf; Schutz gegen DNS-Rebinding)."""
import logging
import os
import time
from pathlib import Path
from urllib.parse import quote

from smartheat_gateway.agent import diagnostics, identity, z2m_config
from smartheat_gateway.agent.api_client import DeviceApiClient
from smartheat_gateway.agent.context import AgentContext
from smartheat_gateway.agent.loop import AgentLoop
from smartheat_gateway.bus import LocalBus
from smartheat_gateway.paths import from_env
from smartheat_gateway.zigbee import ZigbeeMirror


def main() -> None:  # pragma: no cover - Container-Einstieg
    logging.basicConfig(level=logging.INFO)
    paths = from_env()
    ident = identity.load_or_create(paths)
    bus = LocalBus(os.environ.get("SHG_BUS_HOST", "mosquitto"), int(os.environ.get("SHG_BUS_PORT", "1883")), "shg-agent")
    mirror = ZigbeeMirror(bus, time.monotonic)
    mirror.start()
    ctx = AgentContext(paths, bus, mirror, ident)
    ctx.start()
    bus.start()
    z2m_config.ensure(Path(os.environ.get("SHG_ZIGBEE_DIR", "/zigbee2mqtt")), os.environ.get("ZIGBEE_ADAPTER", "ember"))
    loop = AgentLoop(ctx, DeviceApiClient(os.environ["SHG_DEVICE_API_URL"], ident))
    host_status = Path(os.environ.get("SHG_HOST_DIR", "/host")) / "status.json"
    portal = os.environ.get("SHG_PORTAL_BASE_URL", "").rstrip("/")

    def qr_url() -> str | None:
        if not portal:
            return None
        return f"{portal}/#/claim?d={quote(ctx.identity.device_id)}&c={quote(ctx.identity.claim_code)}"

    ctx.diagnostics = lambda: diagnostics.command_snapshot(ctx, loop, host_status)
    diagnostics.start_server(
        int(os.environ.get("SHG_DIAG_PORT", "8080")), lambda: diagnostics.snapshot(ctx, loop, host_status), qr_url,
        diagnostics.parse_hostnames(os.environ.get("SHG_DIAG_HOSTNAMES", "")),
    )
    loop.run()


if __name__ == "__main__":
    main()
