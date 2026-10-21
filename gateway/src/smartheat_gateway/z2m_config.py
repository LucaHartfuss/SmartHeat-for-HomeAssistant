"""Grundkonfiguration von Zigbee2MQTT 2.x (Spec SHG G2 5), einmal geschrieben, wenn configuration.yaml fehlt;
danach gehoert die Datei Zigbee2MQTT (es ersetzt GENERATE selbst); nur user/password im Block mqtt: gleicht der
Init-Schritt danach noch an (sync_credentials, Plan G2b-2). Schreiber ist der Init-Schritt (init.py), nicht mehr
der Agent. Die Datei enthaelt die Zugangsdaten von Zigbee2MQTT am lokalen Bus (0600). permit_join steht nicht darin:
Zigbee2MQTT 2.x kennt die Option nicht mehr, Koppeln ist nach dem Start immer aus (Plan G2a; gegen das echte Image in
G2b pruefen). device_options.retain: true laesst Zigbee2MQTT Geraetewerte retained senden; nach einem Neustart der
Laufzeit gelten sie ueber das last_seen (advanced.last_seen: ISO_8601) sofort, wenn sie juenger als die Frist sind
(ZigbeeMirror, L4-17); bestehende Dateien heilt sync_device_options."""
import re
from pathlib import Path

from smartheat_core import dateien

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
device_options:
  retain: true
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
    dateien.schreiben_text(path, render(adapter, username, password), modus=dateien.PRIVAT)
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
        dateien.schreiben_text(path, "".join(lines), modus=dateien.PRIVAT)
    return changed


_RETAIN_LINE = re.compile(r"^(?P<indent> +)retain: *(?P<value>[^#]*?) *(?P<comment>#.*)?$")


def sync_device_options(zigbee_dir: Path) -> bool:
    """Stellt device_options.retain: true sicher (L4-17): ohne die Zeile sendet Zigbee2MQTT keine retained Geraetewerte,
    und nach einem Neustart der Laufzeit fehlte jeder Fuehlerwert bis zur naechsten Meldung. Fehlt der Block, kommt er
    ans Dateiende; steht er schon in der ersten Einrueckungsebene, wird nur die Zeile retain ergaenzt bzw. von false
    auf true gesetzt. Alles andere (Kommentare, Netzschluessel, andere Optionen) bleibt unberuehrt. True, wenn
    geschrieben wurde; False ohne lesbare Datei und wenn nichts zu tun ist."""
    path = zigbee_dir / "configuration.yaml"
    try:
        lines = path.read_text().splitlines(keepends=True)
    except (OSError, ValueError):  # fehlt, Verzeichnis, keine Rechte, kein UTF-8: Datei bleibt unberuehrt
        return False
    start = next((i for i, line in enumerate(lines) if line.rstrip() == "device_options:"), None)
    if start is None and any(line.startswith("device_options:") for line in lines):
        return False  # andere Schreibweise (z.B. {retain: true}): nicht raten, ein zweiter Schluessel waere Unsinn
    if start is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines += ["device_options:\n", "  retain: true\n"]
    else:
        end = next((i for i in range(start + 1, len(lines)) if _is_content(lines[i]) and not lines[i].startswith(" ")),
                   len(lines))
        body = [i for i in range(start + 1, end) if _is_content(lines[i])]
        indent = "  "
        if body:
            first = lines[body[0]]
            indent = first[: len(first) - len(first.lstrip(" "))]
        for i in body:
            match = _RETAIN_LINE.match(lines[i].rstrip("\n"))
            if match is None or match["indent"] != indent:
                continue
            if _unquote(match["value"]).lower() == "true":
                return False
            comment = f" {match['comment']}" if match["comment"] else ""
            lines[i] = f"{indent}retain: true{comment}\n"
            break
        else:
            lines.insert(start + 1, f"{indent}retain: true\n")
    dateien.schreiben_text(path, "".join(lines), modus=dateien.PRIVAT)
    return True
