"""Grundkonfiguration von Zigbee2MQTT 2.x (Spec SHG G2 5), einmal geschrieben, wenn configuration.yaml fehlt;
danach gehoert die Datei Zigbee2MQTT (es ersetzt GENERATE selbst). permit_join steht nicht darin: Zigbee2MQTT 2.x kennt
die Option nicht mehr, Koppeln ist nach dem Start immer aus (Plan G2a; gegen das echte Image in G2b pruefen). Kein
`retain` fuer Geraete: der Zigbee-Spiegel stempelt retained Werte beim Empfang als frisch."""
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


def ensure(zigbee_dir: Path, adapter: str) -> bool:
    path = zigbee_dir / "configuration.yaml"
    if path.exists():
        return False
    write_text_private(path, TEMPLATE.format(adapter=adapter))
    return True
