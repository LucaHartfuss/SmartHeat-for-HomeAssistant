"""config.yaml: nur Optionen zum Starten, Supervisor-Discovery, kein Optionsmodus."""
import ast
import re
import tomllib
from pathlib import Path

import pytest
import yaml

from heizungsbruecke.version import ADDON_VERSION

CONFIG_YAML_PATH = Path(__file__).resolve().parents[1] / "config.yaml"


def _load_config_yaml() -> dict:
    return yaml.safe_load(CONFIG_YAML_PATH.read_text())


def test_addon_version_constant_matches_config_yaml():
    assert _load_config_yaml()["version"] == ADDON_VERSION


def test_pyproject_liest_die_version_aus_version_py():
    """Keine zweite, veraltende Versionsangabe; setuptools holt sie aus ADDON_VERSION."""
    pyproject = tomllib.loads((CONFIG_YAML_PATH.parent / "pyproject.toml").read_text())
    assert "version" not in pyproject["project"] and pyproject["project"]["dynamic"] == ["version"]
    assert pyproject["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "heizungsbruecke.version.ADDON_VERSION"}


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
    """Der Supervisor haelt stdin des Containers offen; darueber schickt die Integration den lokalen Befehl
    `entfernen <geraet_id>` (stdin_befehl)."""
    assert _load_config_yaml()["stdin"] is True


def test_log_levels_match_the_entry_point():
    from heizungsbruecke.__main__ import LOG_LEVELS
    values = _load_config_yaml()["schema"]["log_level"].removeprefix("list(").removesuffix(")?").split("|")
    assert set(values) == set(LOG_LEVELS)


DOCKERFILE_PATH = Path(__file__).resolve().parents[1] / "Dockerfile"


def test_image_folgt_dem_schema_und_tag_der_version():
    """Der Supervisor zieht das vorgebaute Image ghcr.io/lucahartfuss/<arch>-heizungsbruecke:<version> (`image:` mit
    {arch}, Tag = `version` aus config.yaml) und baut nicht selbst; release.yml pusht genau diese Namen."""
    data = _load_config_yaml()
    assert data["image"] == "ghcr.io/lucahartfuss/{arch}-heizungsbruecke"
    release = (CONFIG_YAML_PATH.parents[1] / ".github" / "workflows" / "release.yml").read_text()
    assert "ghcr.io/lucahartfuss" in release and "-heizungsbruecke" in release
    assert data["arch"] == ["aarch64", "amd64"] and all(arch in release for arch in data["arch"])


def test_dockerfile_traegt_die_ha_labels_mit_version_und_architektur_aus_build_args():
    """Konvention des Supervisors (BUILD_VERSION, BUILD_ARCH): io.hass.version, io.hass.type, io.hass.arch."""
    dockerfile = DOCKERFILE_PATH.read_text()
    assert re.search(r"^ARG BUILD_VERSION\b", dockerfile, flags=re.M)
    assert re.search(r"^ARG BUILD_ARCH\b", dockerfile, flags=re.M)
    labels = dict(re.findall(r'(io\.hass\.\w+)="([^"]*)"', dockerfile))
    assert labels == {"io.hass.version": "${BUILD_VERSION}", "io.hass.type": "addon", "io.hass.arch": "${BUILD_ARCH}"}
    # Die ARGs stehen vor LABEL und nach dem letzten FROM (ein ARG vor FROM gilt nicht in der Stufe).
    assert dockerfile.index("ARG BUILD_VERSION") > dockerfile.index("FROM ")
    assert dockerfile.index("ARG BUILD_ARCH") < dockerfile.index("LABEL ")
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


# --- Rechte und AppArmor (AP-04, SG-17): jedes Recht des Add-ons steht in der Liste mit Grund in docs/module/ha-host.md
RECHTE = {"homeassistant_api", "hassio_api", "discovery", "stdin"}
# Schluessel der config.yaml ohne Rechtewirkung (Beschreibung, Bezug des Images, Startreihenfolge, Optionsschema)
METADATEN = {"name", "version", "slug", "description", "url", "arch", "image", "startup", "schema"}
# Schluessel der Add-on-Konfiguration, die dem Container mehr Rechte oder Zugang geben (Auswahl des Supervisors);
# keiner davon darf vorkommen, ausser den vier Rechten der Liste.
MEHR_RECHTE = {
    "privileged", "full_access", "host_network", "host_pid", "host_ipc", "host_uts", "host_dbus", "docker_api",
    "hassio_role", "auth_api", "devices", "udev", "uart", "usb", "gpio", "kernel_modules", "video", "audio",
    "map", "ingress", "ingress_port", "panel_icon", "ports", "ports_description", "tmpfs", "environment", "init",
    "journald", "realtime", "ipc", "devicetree", "services", "watchdog", "backup", "boot", "webui", "options",
}
APPARMOR_PATH = Path(__file__).resolve().parents[1] / "apparmor.txt"


def _ueberzaehlige_rechte(config: dict) -> set[str]:
    """Schluessel, die weder Metadaten noch eines der vier Rechte der Liste sind."""
    return set(config) - METADATEN - RECHTE


def test_config_rechte_nur_aus_der_liste():
    """Jedes Recht des Add-ons steht in der Liste (homeassistant_api, hassio_api, discovery, stdin) und hat seinen
    Grund in docs/module/ha-host.md. Ein neuer Schluessel (Host-Netz, Geraete, privileged, hassio_role ...) faellt
    hier auf und braucht eine Entscheidung. Gegenprobe: ein Beispiel mit weiteren Rechten wird erkannt."""
    config = _load_config_yaml()
    assert set(config) & RECHTE == RECHTE
    assert _ueberzaehlige_rechte(config) == set()
    assert set(config) & MEHR_RECHTE == set()
    assert _ueberzaehlige_rechte(config | {"host_network": True, "privileged": ["SYS_ADMIN"]}) == {
        "host_network", "privileged"}
    # Die Supervisor-API laeuft mit der Standardrolle: weder hassio_role noch auth_api, kein docker_api, kein privileged.
    assert "hassio_role" not in config and "docker_api" not in config and "privileged" not in config


def test_config_schaltet_apparmor_nicht_ab():
    """`apparmor: false` wuerde das Profil abschalten, `apparmor: <name>` ein anderes erzwingen."""
    assert "apparmor" not in _load_config_yaml()


def _apparmor_regeln(text: str) -> list[str]:
    """Regelzeilen des Profils ohne Kommentare, Leerzeilen, #include, Kopf und Klammer; mit abschliessendem Komma."""
    regeln = []
    for zeile in text.splitlines():
        zeile = zeile.split("#", 1)[0].strip() if not zeile.lstrip().startswith("#include") else ""
        if zeile and not zeile.startswith("profile ") and zeile not in ("{", "}"):
            regeln.append(zeile)
    return regeln


def test_apparmor_profil_vorhanden_und_benannt():
    """Der Supervisor laedt apparmor.txt neben der config.yaml und verlangt genau eine Zeile `profile <name> ...`; er
    ersetzt den Namen selbst. Das Profil heisst wie das Add-on und haelt gekappte Pfade (attach_disconnected) und
    geloeschte Dateien (mediate_deleted) im Profil."""
    text = APPARMOR_PATH.read_text()
    koepfe = re.findall(r"^profile (\S+) flags=\(([^)]*)\) \{$", text, flags=re.M)
    assert [name for name, _ in koepfe] == [_load_config_yaml()["slug"]] == ["heizungsbruecke"]
    assert set(koepfe[0][1].split(",")) == {"attach_disconnected", "mediate_deleted"}
    assert len(re.findall(r"^\s*profile ", text, flags=re.M)) == 1  # kein Unterprofil
    assert text.rstrip().endswith("\n}") and text.splitlines().count("}") == 1  # ein Block, Variablen wie @{pid} zaehlen nicht
    assert "#include <tunables/global>" in text and "#include <abstractions/base>" in text


def _erlaubt(regeln: list[str]) -> list[str]:
    return [regel for regel in regeln if not regel.startswith("deny ")]


def test_apparmor_profil_erlaubt_nur_netz_data_und_lesezugriff():
    """Netz (TCP und UDP, IPv4 und IPv6), /data schreibbar, /app nur lesbar, /tmp fuer das TLS-Verzeichnis; kein
    ptrace, kein mount, keine Capability, kein rohes Netz, kein Schreiben ausserhalb von /data und /tmp."""
    regeln = _apparmor_regeln(APPARMOR_PATH.read_text())
    erlaubt = _erlaubt(regeln)
    for netz in ("network inet stream,", "network inet6 stream,", "network inet dgram,", "network inet6 dgram,"):
        assert netz in erlaubt
    assert "/data/** rw," in erlaubt
    assert "/app/** r," in erlaubt
    for regel in erlaubt:
        pfad, _, rechte = regel.rstrip(",").partition(" ")
        if pfad.startswith("/") and "w" in rechte.split()[0]:
            assert pfad.startswith(("/data/", "/tmp/", "/dev/null")), regel  # nur Schreibziele der Liste
        assert not regel.startswith(("ptrace", "mount", "umount", "pivot_root", "capability", "dbus", "file,", "network,"))
        assert not re.match(r"^network (raw|packet|netlink)", regel) and "ptrace" not in regel
    assert not any(regel.startswith("/app/") and "w" in regel.split()[1] for regel in erlaubt)
    # Ausdruecklich gesperrt (auch gegen spaeter eingefuegte Erlaubnisse): jeder dieser Eintraege steht als deny.
    for sperre in ("deny ptrace,", "deny mount,", "deny umount,", "deny pivot_root,", "deny network raw,",
                   "deny network packet,"):
        assert sperre in regeln


def test_apparmor_pruefung_erkennt_zu_weite_beispiele():
    """Gegenprobe zu den Zusicherungen oben: ein Profil mit ptrace, Capability und Schreibrecht auf /app faellt auf."""
    schlecht = _erlaubt(_apparmor_regeln("profile x flags=(attach_disconnected) {\n  ptrace,\n  capability sys_admin,\n"
                                         "  /app/** rw,\n  mount,\n}\n"))
    assert schlecht == ["ptrace,", "capability sys_admin,", "/app/** rw,", "mount,"]
    assert any(r.startswith(("ptrace", "capability", "mount")) for r in schlecht)
    assert [r for r in schlecht if r.startswith("/app/") and "w" in r.split()[1]] == ["/app/** rw,"]
