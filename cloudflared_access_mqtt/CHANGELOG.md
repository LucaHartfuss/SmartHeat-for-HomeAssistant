# Changelog

Wird im Update-Dialog des Supervisors angezeigt. Pro Version ein Abschnitt `## X.Y.Z`.

## 1.0.2

- Das Zugangs-Token wird nicht mehr als Programmargument übergeben, sondern über Umgebungsvariablen
  (nicht mehr per Prozessliste sichtbar). Fehlt eine Option, bricht das Add-on mit einer klaren
  Fehlermeldung ab, statt den Text „null“ weiterzugeben.
- Basis-Image auf Alpine 3.24 mit festem Stand; auch das cloudflared-Image ist auf einen festen Stand gesetzt.

## 1.0.1

- `boot: auto` entfernt (Standardwert); 32-Bit-Architekturen `armhf`/`armv7` entfernt — Home Assistant unterstützt sie seit 2025.12 nicht mehr.

## 1.0.0

- Erste Version: Cloudflare-Access-TCP-Forwarder, macht den SmartHeat-MQTT-Broker für Home Assistant OS unter `127.0.0.1` erreichbar.
