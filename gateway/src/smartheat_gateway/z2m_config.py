"""Grundkonfiguration von Zigbee2MQTT 2.x (Spec SHG G2 5), einmal geschrieben, wenn configuration.yaml fehlt;
danach gehoert die Datei Zigbee2MQTT (es ersetzt GENERATE selbst). Schreiber ist der Init-Schritt (init.py), nicht mehr
der Agent. Die Datei enthaelt die Zugangsdaten von Zigbee2MQTT am lokalen Bus (0600). permit_join steht nicht darin:
Zigbee2MQTT 2.x kennt die Option nicht mehr, Koppeln ist nach dem Start immer aus (Plan G2a; gegen das echte Image in
G2b pruefen). Zigbee2MQTT soll keine Geraetewerte retained senden; der Spiegel stempelt retained Werte ohnehin nicht
frisch."""
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
