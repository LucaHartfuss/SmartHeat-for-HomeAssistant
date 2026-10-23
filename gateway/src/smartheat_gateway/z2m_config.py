"""Grundkonfiguration von Zigbee2MQTT 2.x, einmal geschrieben, wenn configuration.yaml fehlt;
danach gehoert die Datei Zigbee2MQTT (es ersetzt GENERATE selbst); nur user/password im Block mqtt: (sync_credentials)
und adapter im Block serial: nach einem Stick-Tausch (sync_adapter) gleicht der Init-Schritt
danach noch an. Schreiber ist der Init-Schritt (init.py). Die Datei enthaelt die Zugangsdaten von Zigbee2MQTT am lokalen Bus (0600). permit_join steht nicht darin:
Zigbee2MQTT 2.x kennt die Option nicht mehr, Koppeln ist nach dem Start immer aus. device_options.retain: true laesst Zigbee2MQTT Geraetewerte retained senden; nach einem Neustart der
Laufzeit gelten sie ueber das last_seen (advanced.last_seen: ISO_8601) sofort, wenn sie juenger als die Frist sind
(ZigbeeMirror); bestehende Dateien heilt sync_device_options."""
import logging
import re
from pathlib import Path

from smartheat_core import dateien

logger = logging.getLogger(__name__)

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
    """Gleicht user/password im Block mqtt: an die Zugangsdaten des Init-Schritts an: nach einem
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


_ADAPTER_LINE = re.compile(r"^(?P<indent> +)adapter: *(?P<value>[^#]*?) *(?P<comment>#.*)?$")


def sync_adapter(zigbee_dir: Path, adapter: str) -> bool:
    """Gleicht serial.adapter an den Stick an: nach einem Stick-Tausch (zstack <-> ember) meldete die Datei
    sonst weiter den alten Adapter, und Zigbee2MQTT startete nicht. Aendert nur diese eine Zeile der ersten
    Einrueckungsebene des Blocks serial: (Kommentar am Zeilenende bleibt, fehlt sie, kommt sie direkt unter serial:);
    ohne erkennbaren Block serial: bleibt die Datei unberuehrt. True, wenn geschrieben wurde."""
    path = zigbee_dir / "configuration.yaml"
    try:
        lines = path.read_text().splitlines(keepends=True)
    except (OSError, ValueError):  # fehlt, Verzeichnis, keine Rechte, kein UTF-8: Datei bleibt unberuehrt
        return False
    start = next((i for i, line in enumerate(lines) if line.rstrip() == "serial:"), None)
    if start is None:
        return False
    end = next((i for i in range(start + 1, len(lines)) if _is_content(lines[i]) and not lines[i].startswith(" ")),
               len(lines))
    body = [i for i in range(start + 1, end) if _is_content(lines[i])]
    if not body:
        return False
    first = lines[body[0]]
    indent = first[: len(first) - len(first.lstrip(" "))]
    for i in body:
        match = _ADAPTER_LINE.match(lines[i].rstrip("\n"))
        if match is None or match["indent"] != indent:
            continue
        if _unquote(match["value"]) == adapter:
            return False
        comment = f" {match['comment']}" if match["comment"] else ""
        lines[i] = f"{indent}adapter: {adapter}{comment}\n"
        break
    else:
        lines.insert(start + 1, f"{indent}adapter: {adapter}\n")
    dateien.schreiben_text(path, "".join(lines), modus=dateien.PRIVAT)
    return True


_RETAIN_TRUE_IN_LINE = re.compile(r"retain: *[\"']?true\b", re.IGNORECASE)
_RETAIN_LINE = re.compile(r"^(?P<indent> +)retain: *(?P<value>[^#]*?) *(?P<comment>#.*)?$")


def sync_device_options(zigbee_dir: Path) -> bool:
    """Stellt device_options.retain: true sicher: ohne die Zeile sendet Zigbee2MQTT keine retained Geraetewerte,
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
    if start is None and (kopf := next((line for line in lines if line.startswith("device_options:")), None)):
        # andere Schreibweise ({}, {retain: true}, Kommentar in der Kopfzeile): nicht raten, ein zweiter Schluessel waere
        # Unsinn. Steht retain: true erkennbar schon da, ist nichts zu melden, sonst bliebe die Abweichung unbemerkt offen.
        if not _RETAIN_TRUE_IN_LINE.search(kopf):
            logger.warning("Zigbee2MQTT configuration.yaml: device_options in einer Schreibweise, die nicht heilbar ist, "
                           "retain fehlt moeglicherweise: retain: true unter device_options von Hand ergaenzen")
        return False
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
