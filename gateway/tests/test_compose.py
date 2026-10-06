"""Compose-Datei nach Spec SHG G2 7.2 (statisch geprueft; der Lauf steckt in tests/test_gateway_docker_build.sh)."""
import re
from pathlib import Path

import yaml

GATEWAY = Path(__file__).resolve().parents[1]
COMPOSE = GATEWAY / "compose"


def _services(name="docker-compose.yml") -> dict:
    return yaml.safe_load((COMPOSE / name).read_text())["services"]


def test_hardening_everywhere():
    for name, service in _services().items():
        assert service.get("read_only") is True, name
        assert service.get("cap_drop") == ["ALL"], name
        assert "no-new-privileges:true" in service.get("security_opt", []), name
        assert service.get("restart") == "unless-stopped", name
        assert service["logging"]["options"] == {"max-size": "10m", "max-file": "3"}, name


def test_networks_ports_and_mounts():
    services = _services()
    assert services["mosquitto"]["networks"] == ["bus"] and "ports" not in services["mosquitto"]
    assert services["zigbee2mqtt"]["networks"] == ["bus"]
    assert services["runtime"]["network_mode"] == "service:tunnel"
    assert services["agent"]["ports"] == ["80:8080"]
    assert any(volume.endswith(":/data:ro") for volume in services["tunnel"]["volumes"])
    networks = yaml.safe_load((COMPOSE / "docker-compose.yml").read_text())["networks"]
    assert networks["bus"]["internal"] is True


def test_third_party_images_are_pinned():
    for name in ("mosquitto", "zigbee2mqtt"):
        assert "@sha256:" in _services()[name]["image"]


def test_zigbee2mqtt_runs_as_the_agent_uid():
    # Der Agent schreibt configuration.yaml mit 0600 (z2m_config); Zigbee2MQTT muss sie lesen und umschreiben.
    image_user = re.search(r"^USER\s+(\S+)\s*$", (GATEWAY / "Dockerfile").read_text(), re.MULTILINE)
    assert image_user is not None
    assert _services()["zigbee2mqtt"]["user"] == image_user.group(1)


def test_diagnostics_hostnames_pass_through_and_healthcheck_uses_an_ip_host():
    agent = _services()["agent"]
    assert agent["environment"]["SHG_DIAG_HOSTNAMES"] == "${SHG_DIAG_HOSTNAMES:-}"
    assert "http://127.0.0.1:8080/healthz" in " ".join(agent["healthcheck"]["test"])
