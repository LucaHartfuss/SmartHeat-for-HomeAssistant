"""config.yaml ab 1.0.0 (Spec 5c 1.1/1.2): nur Optionen zum Starten, Supervisor-Discovery, kein Optionsmodus mehr. Die
Pruefungen der frueheren Rollen-, Zugangs- und Intervall-Optionen entfallen mit dem Optionsmodus."""
import ast
from pathlib import Path

import pytest
import yaml

from heizungsbruecke.version import ADDON_VERSION

CONFIG_YAML_PATH = Path(__file__).resolve().parents[1] / "config.yaml"


def _load_config_yaml() -> dict:
    return yaml.safe_load(CONFIG_YAML_PATH.read_text())


def test_addon_version_constant_matches_config_yaml():
    assert _load_config_yaml()["version"] == ADDON_VERSION


def test_slug_and_architectures():
    config = _load_config_yaml()
    assert config["slug"] == "heizungsbruecke"
    assert config["arch"] == ["aarch64", "amd64"]


def test_config_yaml_has_only_start_options():
    data = _load_config_yaml()
    assert "options" not in data  # Standard {} (Add-on-Linter: Schluessel mit Standardwert entfernen)
    assert data["schema"] == {"bootstrap_url": "match(^https://.+)?", "log_level": "list(debug|info|warning|error)?"}
    with pytest.raises(KeyError):
        data["schema"]["tenant_id"]


def test_discovery_and_supervisor_api_without_host_network():
    """Discovery (POST /discovery, Dienst smartheat) und Uhr (GET /host/info) brauchen die Supervisor-API; ausgehend
    MQTT zu IoT Core und die Supervisor-API brauchen das Host-Netz nicht (Nutzer-Entscheidung 2026-10-11)."""
    data = _load_config_yaml()
    assert data["discovery"] == ["smartheat"]
    assert data["hassio_api"] is True and data["homeassistant_api"] is True
    assert "host_network" not in data
    assert data["startup"] == "services"


def test_stdin_only_for_the_local_sign_off():
    """L4-06: der Supervisor haelt stdin des Containers offen; darueber schickt die Integration den lokalen Befehl
    `entfernen <geraet_id>` (stdin_befehl)."""
    assert _load_config_yaml()["stdin"] is True


def test_log_levels_match_the_entry_point():
    from heizungsbruecke.__main__ import LOG_LEVELS
    values = _load_config_yaml()["schema"]["log_level"].removeprefix("list(").removesuffix(")?").split("|")
    assert set(values) == set(LOG_LEVELS)


DOCKERFILE_PATH = Path(__file__).resolve().parents[1] / "Dockerfile"
SRC_PATH = Path(__file__).resolve().parents[1] / "src"


def _offene_ports(config: dict, dockerfile: str) -> list[str]:
    """Was einen Port nach aussen oeffnen wuerde: Schluessel der config.yaml und EXPOSE im Dockerfile."""
    funde = [key for key in ("ports", "ports_description", "host_network") if key in config]
    return funde + [zeile.strip() for zeile in dockerfile.splitlines() if zeile.strip().upper().startswith("EXPOSE")]


def test_neither_config_yaml_nor_dockerfile_declare_a_port():
    """Angreifer im LAN (F-ha-host-32): config.yaml ohne ports, ports_description und host_network, das Dockerfile ohne
    EXPOSE. Gegenprobe: eine Beispiel-Konfiguration mit Port und ein Dockerfile mit EXPOSE werden erkannt."""
    assert _offene_ports(_load_config_yaml(), DOCKERFILE_PATH.read_text()) == []
    beispiel = {"ports": {"8080/tcp": 8080}, "ports_description": {"8080/tcp": "Web"}}
    assert _offene_ports(beispiel, "FROM python\nexpose 8080\n") == ["ports", "ports_description", "expose 8080"]


_SERVER_MODULE = ("http.server", "socketserver", "aiohttp", "wsgiref", "xmlrpc.server")
_SERVER_AUFRUFE = ("bind", "listen", "serve_forever", "start_server", "create_server", "make_server")


def _server_stellen(quelltext: str) -> list[str]:
    """Stellen im Quelltext, an denen ein Server-Socket entstuende: Import eines Server-Moduls, Aufruf von bind, listen,
    serve_forever, start_server, create_server oder make_server, die Klasse HTTPServer. Kommentare zaehlen nicht."""
    funde = []
    for knoten in ast.walk(ast.parse(quelltext)):
        if isinstance(knoten, ast.Import):
            funde += [alias.name for alias in knoten.names if alias.name.startswith(_SERVER_MODULE)]
        elif isinstance(knoten, ast.ImportFrom) and (knoten.module or "").startswith(_SERVER_MODULE):
            funde.append(knoten.module or "")
        elif isinstance(knoten, ast.Call) and isinstance(knoten.func, ast.Attribute) and knoten.func.attr in _SERVER_AUFRUFE:
            funde.append(knoten.func.attr)
        elif isinstance(knoten, ast.Name | ast.Attribute) and "HTTPServer" in (getattr(knoten, "id", None) or
                                                                               getattr(knoten, "attr", "")):
            funde.append("HTTPServer")
    return funde


def test_no_server_socket_in_the_addon_code():
    """Angreifer im LAN (F-ha-host-32): kein Modul unter heizungsbruecke/src horcht (kein HTTP-Server, kein Listener).
    Gegenprobe: ein Beispieltext mit jedem dieser Bausteine wird erkannt, ein Kommentar nicht."""
    dateien = sorted(SRC_PATH.rglob("*.py"))
    assert len(dateien) > 40  # der ganze Quelltext (heizungsbruecke, smartheat_core, _device, _runtime)
    funde = {str(pfad.relative_to(SRC_PATH)): stellen for pfad in dateien
             if (stellen := _server_stellen(pfad.read_text()))}
    assert funde == {}
    beispiel = ("import socket\nimport socketserver\nfrom http.server import HTTPServer\nfrom aiohttp import web\n"
                "s = socket.socket()\ns.bind(('', 80))\ns.listen()\nHTTPServer(('', 8080), None).serve_forever()\n"
                "# s.bind(('', 81)) nur ein Kommentar\n")
    assert sorted(_server_stellen(beispiel)) == sorted(
        ["socketserver", "http.server", "aiohttp", "bind", "listen", "HTTPServer", "serve_forever"])
