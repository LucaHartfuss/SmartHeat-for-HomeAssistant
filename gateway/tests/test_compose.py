"""Compose-Datei nach Spec SHG G2 7.2, ab 0.6.0 Spec 5b 5.3 (statisch geprueft; der Lauf steckt in
tests/test_gateway_docker_build.sh)."""
import ast
import re
from pathlib import Path

import yaml

GATEWAY = Path(__file__).resolve().parents[1]
COMPOSE = GATEWAY / "compose"


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader, der das Compose-Tag !override kennt (Overlay ersetzt die Liste der Basisdatei)."""


_ComposeLoader.add_constructor("!override", lambda loader, node: loader.construct_sequence(node))


def _services(name="docker-compose.yml") -> dict:
    return yaml.load((COMPOSE / name).read_text(), Loader=_ComposeLoader)["services"]  # SafeLoader-Unterklasse


def test_hardening_everywhere():
    for name, service in _services().items():
        assert service.get("read_only") is True, name
        assert service.get("cap_drop") == ["ALL"], name
        assert "no-new-privileges:true" in service.get("security_opt", []), name
        assert service.get("restart") == ("no" if name == "init" else "unless-stopped"), name
        assert service["logging"]["options"] == {"max-size": "10m", "max-file": "3"}, name


def test_one_gateway_container_besides_init():
    assert set(_services()) == {"init", "runtime", "mosquitto", "zigbee2mqtt"}


def test_networks_ports_and_mounts():
    services = _services()
    assert services["mosquitto"]["networks"] == ["bus"] and "ports" not in services["mosquitto"]
    assert services["zigbee2mqtt"]["networks"] == ["bus"]
    runtime = services["runtime"]
    assert runtime["networks"] == ["bus", "wan"] and runtime["ports"] == ["80:8080"]
    assert "network_mode" not in runtime
    targets = [_container_target(v) for v in runtime["volumes"]]
    assert targets == ["/data", "/host", "/run/shg-bus"]
    assert runtime["volumes"][1].endswith(":/host:ro")
    networks = yaml.safe_load((COMPOSE / "docker-compose.yml").read_text())["networks"]
    assert networks["bus"]["internal"] is True


def test_third_party_images_are_pinned():
    for name in ("mosquitto", "zigbee2mqtt"):
        assert "@sha256:" in _services()[name]["image"]


def test_zigbee2mqtt_runs_as_the_gateway_uid():
    # Der Init-Schritt schreibt configuration.yaml mit 0600 (als Gateway-Benutzer); Zigbee2MQTT muss sie lesen und
    # umschreiben.
    image_user = re.search(r"^USER\s+(\S+)\s*$", (GATEWAY / "Dockerfile").read_text(), re.MULTILINE)
    assert image_user is not None
    assert _services()["zigbee2mqtt"]["user"] == image_user.group(1)


def test_runtime_environment_and_healthcheck():
    runtime = _services()["runtime"]
    env = runtime["environment"]
    assert env["SHG_DEVICE_API_URL"] == "${SHG_DEVICE_API_URL:?SHG_DEVICE_API_URL fehlt}"
    assert env["SHG_DIAG_HOSTNAMES"] == "${SHG_DIAG_HOSTNAMES:-}"
    assert env["SHG_HOST_DIR"] == "/host" and env["SHG_DATA_DIR"] == "/data"
    assert "SHG_BUS_LOST_EXIT_SECONDS" not in env  # Netz-Wache entfaellt mit dem Tunnel
    test = " ".join(runtime["healthcheck"]["test"])
    assert "http://127.0.0.1:8080/healthz" in test and "/tmp/shg-runtime-alive" in test


def test_runtime_stops_only_after_the_device_thread_has_disconnected():
    """runtime_main trennt beim Ende sauber (MQTT-DISCONNECT) und wartet bis JOIN_SECONDS auf den Geraete-Thread;
    Dockers Standard-Frist (10 s) ist genau so lang, SIGKILL kaeme dann vor dem Trennen (DUPLICATE_CLIENTID)."""
    from smartheat_gateway import runtime_main

    grace = _services()["runtime"]["stop_grace_period"]
    assert re.fullmatch(r"\d+s", grace), grace
    assert int(grace[:-1]) >= runtime_main.JOIN_SECONDS + 5


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


def test_runtime_gets_only_its_own_bus_credentials_and_sees_the_device_keys():
    runtime = _services()["runtime"]
    assert runtime["environment"]["SHG_BUS_CREDENTIALS"] == "/run/shg-bus/bus.json"
    mounts = [v for v in runtime["volumes"] if isinstance(v, str) and "/bus/credentials/" in v]
    assert mounts == ["${SHG_ROOT:-/var/lib/smartheat}/bus/credentials/runtime:/run/shg-bus:ro"]
    assert not [v for v in runtime["volumes"] if isinstance(v, dict)]  # keine tmpfs-Masken mehr


def _copy_sources(dockerfile: str) -> list[str]:
    """Quellen der COPY-Zeilen aus dem Build-Kontext (COPY --from=... kommt aus einer anderen Stufe)."""
    sources: list[str] = []
    for line in dockerfile.splitlines():
        parts = line.split()
        if parts[:1] == ["COPY"] and not any(part.startswith("--from=") for part in parts):
            sources.extend(part for part in parts[1:-1] if not part.startswith("--"))
    return sources


def _whitelisted(source: str, ignore: str) -> bool:
    """Steht die Quelle (oder ein Elternordner) als Ausnahme !pfad in der .dockerignore (die mit * beginnt)?"""
    allowed = [line[1:].rstrip("/") for line in ignore.splitlines() if line.startswith("!")]
    return any(source == entry or source.startswith(entry + "/") for entry in allowed)


def test_the_image_starts_the_runtime_and_carries_the_device_core():
    dockerfile = (GATEWAY / "Dockerfile").read_text()
    assert 'CMD ["python", "-m", "smartheat_gateway.runtime_main"]' in dockerfile
    assert "COPY heizungsbruecke/src/smartheat_device ./src/smartheat_device" in dockerfile
    assert "cloudflared" not in dockerfile
    # Plan-Luecke B4: was die .dockerignore nicht freigibt, fehlt im Build-Kontext, und COPY scheitert erst beim Bau.
    ignore = (GATEWAY / "Dockerfile.dockerignore").read_text()
    assert ignore.splitlines()[0] == "*"
    sources = _copy_sources(dockerfile)
    assert "heizungsbruecke/src/smartheat_device" in sources
    assert [source for source in sources if not _whitelisted(source, ignore)] == []


def test_the_image_carries_every_package_the_gateway_imports():
    """Jedes smartheat_*-Paket, das der Gateway-Code importiert, liegt im Image (sonst ImportError erst im Container)."""
    imported: set[str] = set()
    for path in (GATEWAY / "src" / "smartheat_gateway").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module]
            else:
                continue
            imported.update(name.split(".")[0] for name in names if name.startswith("smartheat_"))
    copied = {source.rsplit("/", 1)[-1] for source in _copy_sources((GATEWAY / "Dockerfile").read_text())}
    assert imported <= copied, sorted(imported - copied)


def test_dev_overlay_only_adjusts_existing_services_and_adds_the_fakes():
    dev = _services("docker-compose.dev.yml")
    assert set(dev) - set(_services()) == {"fake-zigbee2mqtt", "fake-bootstrap", "fake-broker"}
    assert dev["runtime"]["ports"] == ["127.0.0.1:8080:8080"]


def test_dev_overlay_brings_bootstrap_and_broker_but_no_device_api():
    services = _services("docker-compose.dev.yml")
    assert {"fake-bootstrap", "fake-broker"} <= set(services)
    assert "fake-device-api" not in services and "agent" not in services and "tunnel" not in services
    # Der Fake-Broker ist der Endpunkt aus der Bootstrap-Antwort; der Geraetekern startet erst nach dem Fake-Bootstrap.
    assert services["fake-bootstrap"]["environment"]["SHG_FAKE_MQTT_HOST"] == "fake-broker"
    assert services["runtime"]["depends_on"]["fake-bootstrap"] == {"condition": "service_healthy"}
    assert "@sha256:" in services["fake-broker"]["image"]


def test_mosquitto_requires_login():
    conf = (COMPOSE / "mosquitto.conf").read_text()
    assert "allow_anonymous false" in conf
    assert "password_file /mosquitto/config/bus/passwd" in conf and "acl_file /mosquitto/config/bus/acl" in conf
    assert _services()["mosquitto"]["user"] == "1000:1000"
