"""Grundkonfiguration von Zigbee2MQTT 2.x (Spec SHG G2 5), einmal geschrieben, wenn configuration.yaml fehlt;
danach gehoert die Datei Zigbee2MQTT (es ersetzt GENERATE selbst); nur user/password im Block mqtt: gleicht der
Init-Schritt danach noch an (sync_credentials, Plan G2b-2). Schreiber ist der Init-Schritt (init.py), nicht mehr
der Agent. Die Datei enthaelt die Zugangsdaten von Zigbee2MQTT am lokalen Bus (0600). permit_join steht nicht darin:
Zigbee2MQTT 2.x kennt die Option nicht mehr, Koppeln ist nach dem Start immer aus (Plan G2a; gegen das echte Image in
G2b pruefen). Zigbee2MQTT soll keine Geraetewerte retained senden; der Spiegel stempelt retained Werte ohnehin nicht
frisch."""
import re
from pathlib import Path

from smartheat_gateway.files import write_text_private

TEMPLATE = """\
version: 4
homeassistant:
  enabled: false
frontend:
  enabled: false
mqtt:
  base_topic: zigbee2mqtt
  server: mqtt://mosquitto:1883
  user: {username}
  password: "{password}"
serial:
  port: /dev/zigbee
  adapter: {adapter}
advanced:
  network_key: GENERATE
  pan_id: GENERATE
  ext_pan_id: GENERATE
  last_seen: ISO_8601
  log_level: warning
"""


def render(adapter: str, username: str, password: str) -> str:
    return TEMPLATE.format(adapter=adapter, username=username, password=password)


def ensure(zigbee_dir: Path, adapter: str, username: str, password: str) -> bool:
    path = zigbee_dir / "configuration.yaml"
    if path.exists():
        return False
    write_text_private(path, render(adapter, username, password))
    return True


_KEY_LINE = re.compile(r"^(?P<indent> +)(?P<key>user|password): *(?P<value>.*?) *$")


def _is_content(line: str) -> bool:
    """Weder leer noch Kommentar: nur solche Zeilen zaehlen fuer Blockende und Einrueckung."""
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def sync_credentials(zigbee_dir: Path, username: str, password: str) -> bool:
    """Gleicht user/password im Block mqtt: an die Zugangsdaten des Init-Schritts an (Plan G2b-2 Task 3): nach einem
    Schluesseltausch oder einer zurueckgespielten Sicherung verbaende sich Zigbee2MQTT sonst nie. Aendert nur diese
    zwei Zeilen der ersten Einrueckungsebene (Netzschluessel und alles andere bleiben, wie Zigbee2MQTT sie geschrieben
    hat); ohne erkennbaren Block mqtt: bleibt die Datei unberuehrt. True, wenn geschrieben wurde."""
    path = zigbee_dir / "configuration.yaml"
    try:
        lines = path.read_text().splitlines(keepends=True)
    except (OSError, ValueError):  # fehlt, Verzeichnis, keine Rechte, kein UTF-8: Datei bleibt unberuehrt
        return False
    start = next((i for i, line in enumerate(lines) if line.rstrip() == "mqtt:"), None)
    if start is None:
        return False
    end = next((i for i in range(start + 1, len(lines)) if _is_content(lines[i]) and not lines[i].startswith(" ")),
               len(lines))
    body = [i for i in range(start + 1, end) if _is_content(lines[i])]
    if not body:
        return False
    first = lines[body[0]]
    indent = first[: len(first) - len(first.lstrip(" "))]
    wanted = {"user": username, "password": password}
    rendered = {"user": username, "password": f'"{password}"'}
    seen: set[str] = set()
    changed = False
    for i in body:
        match = _KEY_LINE.match(lines[i].rstrip("\n"))
        if match is None or match["indent"] != indent:
            continue
        key = match["key"]
        seen.add(key)
        if _unquote(match["value"]) != wanted[key]:
            lines[i] = f"{indent}{key}: {rendered[key]}\n"
            changed = True
    for key in ("password", "user"):  # fehlende Zeilen direkt unter mqtt: einfuegen (user landet zuerst)
        if key not in seen:
            lines.insert(start + 1, f"{indent}{key}: {rendered[key]}\n")
            changed = True
    if changed:
        write_text_private(path, "".join(lines))
    return changed
