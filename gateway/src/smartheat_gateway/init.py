"""Init-Schritt des Gateways: einmaliger Compose-Dienst vor allen anderen. Erzeugt, was
fehlt: zufaellige Passwoerter je Dienst am lokalen Bus, Mosquitto-Passwortdatei ($7$, PBKDF2-SHA512) und ACL, die
Zigbee2MQTT-Grundkonfiguration mit Zugangsdaten und den Ordner data/device (0700, Geraetekern). Heilt ausserdem Drift:
eine Passwortdatei, die nicht zu den Zugangsdateien passt, wird neu aufgebaut, und die Zugangsdaten in einer bestehenden
Zigbee2MQTT-Konfiguration werden angeglichen, ebenso der Zigbee-Adapter nach einem Stick-Tausch (Host-Datei zigbee_adapter,
wirkt mit dem naechsten Lauf dieses Schritts, das heisst `install.sh` erneut). Idempotent: ein zweiter Lauf
schreibt nichts. Ein Schreiber je Datei: bus/ und configuration.yaml (ganz nur wenn sie fehlt, sonst nur
user/password und serial.adapter) gehoeren diesem Schritt. Passwoerter erscheinen
nie im Log (nur Dateinamen)."""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from collections.abc import Callable
from pathlib import Path

from smartheat_core import dateien
from smartheat_gateway import z2m_config

logger = logging.getLogger(__name__)

SERVICES = ("runtime", "zigbee2mqtt")
DEFAULT_ADAPTER = "ember"
_ADAPTER_NAME = re.compile(r"[a-z0-9_]+")  # Zigbee2MQTT-Adapter: zstack, ember, deconz, ...; kein Text fuer die Datei
ITERATIONS = 101
SALT_BYTES = 12
MAX_VERIFY_ITERATIONS = 1_000_000  # Obergrenze beim Pruefen: ein feindlicher Eintrag soll nicht ewig rechnen
ACL_RULES: dict[str, tuple[tuple[str, str], ...]] = {
    # Ab 0.6.0 traegt der lokale Bus nur noch Zigbee2MQTT; Status, Raum und Meldungen bleiben im Prozess.
    "runtime": (
        ("read", "zigbee2mqtt/#"), ("write", "zigbee2mqtt/+/set"), ("write", "zigbee2mqtt/bridge/request/#"),
    ),
    "zigbee2mqtt": (("readwrite", "zigbee2mqtt/#"),),
}


def mosquitto_hash(password: str, salt: bytes | None = None, iterations: int = ITERATIONS) -> str:
    salt = salt if salt is not None else secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, iterations, 64)
    return f"$7${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def mosquitto_verify(password: str, line_hash: str) -> bool:
    """Passt das Passwort zum $7$-Eintrag (PBKDF2-SHA512) der Passwortdatei? Unlesbare Eintraege passen nie."""
    try:
        _, seven, iterations, salt_b64, digest_b64 = line_hash.split("$")
        if seven != "7" or int(iterations) > MAX_VERIFY_ITERATIONS:
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        digest = base64.b64decode(digest_b64, validate=True)
        computed = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, int(iterations), len(digest))
    except (ValueError, OverflowError):  # auch binascii.Error
        return False
    return hmac.compare_digest(computed, digest)


def _passwd_matches(path: Path, passwords: dict[str, str]) -> bool:
    """Hat die Passwortdatei genau einen passenden Eintrag je Dienst?"""
    try:
        lines = path.read_text().splitlines()
    except (OSError, ValueError):  # auch UnicodeDecodeError
        return False
    entries = dict(line.split(":", 1) for line in lines if ":" in line)
    return set(entries) == set(SERVICES) and all(mosquitto_verify(passwords[s], entries[s]) for s in SERVICES)


def render_acl() -> str:
    return "\n\n".join(
        "\n".join([f"user {user}", *(f"topic {access} {topic}" for access, topic in rules)])
        for user, rules in ACL_RULES.items()
    ) + "\n"


def _acl_matches(path: Path) -> bool:
    """Entspricht die ACL-Datei der gerenderten? Unlesbar oder kein UTF-8 gilt als abweichend."""
    try:
        return path.read_text() == render_acl()
    except (OSError, ValueError):  # auch FileNotFoundError, UnicodeDecodeError
        return False


def adapter(host_dir: Path) -> str | None:
    """Adapter des eingesteckten Sticks aus der Host-Datei zigbee_adapter (schreibt die udev-Regel). None ohne Datei,
    ohne Inhalt oder bei einem Inhalt, der kein Adaptername ist: dann gleicht der Init-Schritt nichts an."""
    try:
        value = (host_dir / "zigbee_adapter").read_text().strip()
    except (OSError, ValueError):
        return None
    if _ADAPTER_NAME.fullmatch(value) is None:
        if value:
            logger.warning("Host-Datei zigbee_adapter: kein Adaptername, der Adapter wird nicht angeglichen")
        return None
    return value


def _credentials(bus_dir: Path, token: Callable[..., str], written: list[str]) -> tuple[dict[str, str], bool]:
    """Passwoerter je Dienst aus den vorhandenen Dateien; fehlende oder unlesbare werden neu erzeugt."""
    passwords: dict[str, str] = {}
    created = False
    for service in SERVICES:
        path = bus_dir / "credentials" / service / "bus.json"
        try:
            passwords[service] = str(json.loads(path.read_text())["password"])
            continue
        except (OSError, ValueError, KeyError, TypeError):
            pass
        passwords[service] = token(32)
        dateien.schreiben_text(path, json.dumps({"username": service, "password": passwords[service]}), modus=dateien.PRIVAT)
        written.append(f"bus/credentials/{service}/bus.json")
        created = True
    return passwords, created


def ensure(bus_dir: Path, zigbee_dir: Path, data_dir: Path, adapter_name: str | None,
           token: Callable[..., str] = secrets.token_urlsafe, default_adapter: str = DEFAULT_ADAPTER) -> list[str]:
    """`adapter_name` None (keine Host-Datei): kein Abgleich, `default_adapter` gilt nur beim ersten Schreiben."""
    written: list[str] = []
    passwords, created = _credentials(bus_dir, token, written)
    passwd = bus_dir / "mosquitto" / "passwd"
    if created or not _passwd_matches(passwd, passwords):
        dateien.schreiben_text(passwd, "".join(f"{s}:{mosquitto_hash(passwords[s])}\n" for s in SERVICES), modus=dateien.PRIVAT)
        written.append("bus/mosquitto/passwd")
    acl = bus_dir / "mosquitto" / "acl"
    if not _acl_matches(acl):
        dateien.schreiben_text(acl, render_acl(), modus=dateien.PRIVAT)
        written.append("bus/mosquitto/acl")
    if z2m_config.ensure(zigbee_dir, adapter_name or default_adapter, "zigbee2mqtt", passwords["zigbee2mqtt"]):
        written.append("zigbee2mqtt/configuration.yaml")
    elif z2m_config.sync_credentials(zigbee_dir, "zigbee2mqtt", passwords["zigbee2mqtt"]):
        written.append("zigbee2mqtt/configuration.yaml (Zugangsdaten)")
    if adapter_name is not None and z2m_config.sync_adapter(zigbee_dir, adapter_name):
        written.append("zigbee2mqtt/configuration.yaml (Adapter)")
    if z2m_config.sync_device_options(zigbee_dir):
        written.append("zigbee2mqtt/configuration.yaml (retain)")
    for name in ("device",):
        path = data_dir / name
        if not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(0o700)
            written.append(f"data/{name}/")
    return written


def main() -> None:  # pragma: no cover - Container-Einstieg
    logging.basicConfig(level=logging.INFO)
    written = ensure(
        Path(os.environ.get("SHG_BUS_DIR", "/bus")), Path(os.environ.get("SHG_ZIGBEE_DIR", "/zigbee2mqtt")),
        Path(os.environ.get("SHG_DATA_DIR", "/data")),
        adapter(Path(os.environ.get("SHG_HOST_DIR", "/host"))),
        default_adapter=os.environ.get("ZIGBEE_ADAPTER", DEFAULT_ADAPTER),
    )
    logger.info("Init: %s", ", ".join(written) if written else "nichts zu tun")


if __name__ == "__main__":
    main()
