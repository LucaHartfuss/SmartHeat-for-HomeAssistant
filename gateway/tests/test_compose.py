"""Compose-Datei (statisch geprueft; der Lauf steckt in
tests/test_gateway_docker_build.sh)."""
import ast
import re
from pathlib import Path

import yaml

PUBLISHED_PORT = "0.0.0.0:80:8080"  # nur IPv4 (P-C2 b)
HEALTH_PUBLISHED_PORT = "127.0.0.1:8081:8081"  # nur /healthz, nur Loopback des Hosts (S-10)
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
        # journald statt json-file: ein Stromausfall laesst keine beschaedigte Logdatei zurueck, Groesse und
        # Rotation regelt das Journal des Hosts; kein Dienst ueberschreibt den Anker mit einem eigenen Block.
        assert service["logging"] == {"driver": "journald", "options": {"tag": "{{.Name}}"}}, name


def test_one_gateway_container_besides_init():
    assert set(_services()) == {"init", "runtime", "mosquitto", "zigbee2mqtt"}


def test_networks_ports_and_mounts():
    services = _services()
    assert services["mosquitto"]["networks"] == ["bus"] and "ports" not in services["mosquitto"]
    assert services["zigbee2mqtt"]["networks"] == ["bus"]
    runtime = services["runtime"]
    assert runtime["networks"] == ["bus", "wan"] and runtime["ports"] == [PUBLISHED_PORT, HEALTH_PUBLISHED_PORT]
    assert "network_mode" not in runtime
    targets = [_container_target(v) for v in runtime["volumes"]]
    assert targets == ["/data", "/host", "/run/shg-bus"]
    assert runtime["volumes"][1].endswith(":/host:ro")
    networks = yaml.safe_load((COMPOSE / "docker-compose.yml").read_text())["networks"]
    assert networks["bus"]["internal"] is True


def test_the_diagnostic_port_is_published_on_ipv4_only():
    """Ohne Host-Adresse veroeffentlicht Docker den Port auch auf `[::]:80` (docker-proxy), die
    Firewall-Regel `tcp dport 80 accept` gaelte dann auch fuer IPv6. Mit `0.0.0.0` als Host-Adresse lauscht nur IPv4."""
    ports = _services()["runtime"]["ports"]
    assert ports == ["0.0.0.0:80:8080", "127.0.0.1:8081:8081"]


def test_only_the_runtime_publishes_a_port_and_only_80_to_8080():
    """Docker-Ports laufen per DNAT an der input-Kette der Firewall vorbei: ueber alle Dienste der Compose-Datei (nicht
    nur die bekannten) hat nur `runtime` `ports`, genau `0.0.0.0:80:8080`; Mosquitto, Zigbee2MQTT und init veroeffentlichen
    nichts. Dazu `127.0.0.1:8081:8081` (nur /healthz, nur Loopback des Hosts)."""
    services = _services()
    assert {name for name, service in services.items() if "ports" in service} == {"runtime"}
    assert services["runtime"]["ports"] == [PUBLISHED_PORT, HEALTH_PUBLISHED_PORT]
    assert {"init", "mosquitto", "zigbee2mqtt"} <= set(services)


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
    assert {"SHG_HOST_DIR", "SHG_DATA_DIR"}.isdisjoint(env)  # Die Ordner sind die Standardwerte des Codes (/host, /data)
    assert "SHG_BUS_LOST_EXIT_SECONDS" not in env  # Netz-Wache entfaellt mit dem Tunnel
    test = " ".join(runtime["healthcheck"]["test"])
    from smartheat_gateway import runtime_main

    # Der Healthcheck fragt den eigenen Gesundheitsserver, nicht die Diagnoseseite, deren Plaetze das LAN fuellen kann
    assert f"http://127.0.0.1:{runtime_main.HEALTH_PORT}/healthz" in test and "/tmp/shg-runtime-alive" in test
    assert PUBLISHED_PORT.endswith(f":{runtime_main.DIAG_PORT}")  # DIAG_PORT ist der Container-Port der Compose-Datei
    assert f"127.0.0.1:{runtime_main.HEALTH_PORT}:{runtime_main.HEALTH_PORT}" == HEALTH_PUBLISHED_PORT
    assert runtime_main.HEALTH_PORT != runtime_main.DIAG_PORT


def test_runtime_stops_only_after_the_device_thread_has_disconnected():
    """prozess.beenden trennt beim Ende sauber (MQTT-DISCONNECT) und wartet bis JOIN_SECONDS auf den Geraete-Thread;
    Dockers Standard-Frist (10 s) ist genau so lang, SIGKILL kaeme dann vor dem Trennen (DUPLICATE_CLIENTID)."""
    from smartheat_device import prozess

    grace = _services()["runtime"]["stop_grace_period"]
    assert re.fullmatch(r"\d+s", grace), grace
    assert int(grace[:-1]) >= prozess.JOIN_SECONDS + 5


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
    assert set(init["environment"]) == {"ZIGBEE_ADAPTER"}  # Die Ordner sind die Standardwerte von init.py


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
    assert dev["runtime"]["ports"] == ["127.0.0.1:8080:8080", "127.0.0.1:8081:8081"]


def test_dev_overlay_brings_bootstrap_and_broker():
    services = _services("docker-compose.dev.yml")
    assert {"fake-bootstrap", "fake-broker"} <= set(services)
    # Der Fake-Broker ist der Endpunkt aus der Bootstrap-Antwort; der Geraetekern startet erst nach dem Fake-Bootstrap.
    assert services["fake-bootstrap"]["environment"]["SHG_FAKE_MQTT_HOST"] == "fake-broker"
    assert services["runtime"]["depends_on"]["fake-bootstrap"] == {"condition": "service_healthy"}
    assert "@sha256:" in services["fake-broker"]["image"]


def test_mosquitto_requires_login():
    conf = (COMPOSE / "mosquitto.conf").read_text()
    assert "allow_anonymous false" in conf
    assert "password_file /mosquitto/config/bus/passwd" in conf and "acl_file /mosquitto/config/bus/acl" in conf
    assert _services()["mosquitto"]["user"] == "1000:1000"
