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
        assert service.get("restart") == ("no" if name == "init" else "unless-stopped"), name
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
    # Der Init-Schritt schreibt configuration.yaml mit 0600 (als Agent-Benutzer); Zigbee2MQTT muss sie lesen und umschreiben.
    image_user = re.search(r"^USER\s+(\S+)\s*$", (GATEWAY / "Dockerfile").read_text(), re.MULTILINE)
    assert image_user is not None
    assert _services()["zigbee2mqtt"]["user"] == image_user.group(1)


def test_diagnostics_hostnames_pass_through_and_healthcheck_uses_an_ip_host():
    agent = _services()["agent"]
    assert agent["environment"]["SHG_DIAG_HOSTNAMES"] == "${SHG_DIAG_HOSTNAMES:-}"
    assert "http://127.0.0.1:8080/healthz" in " ".join(agent["healthcheck"]["test"])


def _depends(service: dict) -> dict:
    deps = service.get("depends_on", {})
    return deps if isinstance(deps, dict) else {name: {} for name in deps}


def test_everything_waits_for_init():
    for name, service in _services().items():
        if name == "init":
            continue
        assert _depends(service).get("init", {}).get("condition") == "service_completed_successfully", name


def _container_target(volume: str) -> str:
    # Die Variable ${SHG_ROOT:-/var/lib/smartheat} enthaelt selbst einen Doppelpunkt: erst hinter "}" trennen.
    return volume.split("}", 1)[-1].split(":")[1]


def test_init_has_no_network_and_writes_bus_zigbee_and_data():
    init = _services()["init"]
    assert init["network_mode"] == "none"
    assert "networks" not in init and "ports" not in init
    targets = [_container_target(volume) for volume in init["volumes"]]
    assert targets == ["/bus", "/zigbee2mqtt", "/data", "/host"]


def test_each_gateway_service_gets_only_its_own_bus_credentials():
    for name in ("agent", "runtime"):
        service = _services()[name]
        assert service["environment"]["SHG_BUS_CREDENTIALS"] == "/run/shg-bus/bus.json"
        mounts = [v for v in service["volumes"] if isinstance(v, str) and "/bus/credentials/" in v]
        assert mounts == [f"${{SHG_ROOT:-/var/lib/smartheat}}/bus/credentials/{name}:/run/shg-bus:ro"]


def test_device_keys_are_masked_in_tunnel_and_runtime():
    for name in ("tunnel", "runtime"):
        masks = {v["target"] for v in _services()[name]["volumes"] if isinstance(v, dict) and v.get("type") == "tmpfs"}
        assert masks == {"/data/device", "/data/agent"}, name


def test_mosquitto_requires_login():
    conf = (COMPOSE / "mosquitto.conf").read_text()
    assert "allow_anonymous false" in conf
    assert "password_file /mosquitto/config/bus/passwd" in conf and "acl_file /mosquitto/config/bus/acl" in conf
    assert _services()["mosquitto"]["user"] == "1000:1000"


def test_runtime_bus_watch_is_configurable():
    env = _services()["runtime"]["environment"]
    assert env["SHG_BUS_LOST_EXIT_SECONDS"] == "${SHG_BUS_LOST_EXIT_SECONDS:-300}"
