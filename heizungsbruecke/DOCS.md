# Heizungsbruecke

Liest konfigurierte Home-Assistant-Entities (Referenzraum, Aussentemperatur,
aktuelle Heizkurve/Niveau) und meldet sie generisch an den SmartHeat-Server.
Schreibt vom Server empfangene Sollwerte zurueck, geclamped gegen die
konfigurierten Sicherheitsgrenzen. Enthaelt eine lokale Boost-Hysterese,
die unabhaengig vom Server eingreift, wenn der Referenzraum mehr als
`boost_threshold_k` unter der Zieltemperatur liegt.

## Voraussetzungen

- Das Add-on **Cloudflared Access TCP-Bridge** (`cloudflared_access_mqtt`, aus
  demselben Repository) muss installiert, konfiguriert und **gestartet** sein,
  bevor dieses Add-on gestartet wird — es stellt den MQTT-Broker unter
  `127.0.0.1:<local_port>` bereit.
- Der `mqtt_port` dieses Add-ons **muss** exakt dem `local_port` von
  `cloudflared_access_mqtt` entsprechen (Standard fuer beide: `18830`).
- Dieses Add-on benoetigt `homeassistant_api: true` (Zugriff auf die
  Home-Assistant-Core-API, um Entity-Zustaende zu lesen/Sollwerte zu setzen)
  und `host_network: true` (um `cloudflared_access_mqtt`s Broker unter
  `127.0.0.1` tatsaechlich erreichen zu koennen) — beides ist in `config.yaml`
  bereits gesetzt, wird hier nur der Vollstaendigkeit halber dokumentiert.

## Konfiguration

- `tenant_id`: eindeutige Kennung dieser Anlage (bestimmt das MQTT-Topic-Praefix
  `smartheat/<tenant_id>/...`).
- `profile`: Profil dieser Anlage (Pflicht). Bestimmt, welche `entity_*`-Felder
  erforderlich sind. Aktuell unterstützte Profile:
  - `weishaupt_waermepumpe_fussbodenheizung`: Wärmepumpe mit Fußbodenheizung
  - `vaillant_gastherme_heizkoerper`: Vaillant-Gastherme mit Heizkörpern
- `mqtt_host`: Hostname/IP des MQTT-Brokers, wie er aus diesem Add-on heraus
  erreichbar ist. Standard `127.0.0.1` (der von `cloudflared_access_mqtt`
  bereitgestellte lokale Broker-Zugang).
- `mqtt_port`: Port des MQTT-Brokers. **Muss** mit `cloudflared_access_mqtt`s
  `local_port` uebereinstimmen. Standard `18830`.
- `entity_room_actual` / `entity_room_target`: Ist-/Soll-Temperatur des
  Referenzraums (Pflicht).
- `entity_curve_current` / `entity_offset_current`: Ziel-Entities fuer
  Heizkurve/Niveau (Pflicht) — dorthin schreibt das Add-on sowohl vom Server
  empfangene Sollwerte als auch (im Boost-Fall) die lokalen Boost-Werte.
- `entity_outdoor_temp`: optional, Aussentemperatur-Sensor.
- `entity_room_day_avg` / `entity_room_night_avg`: Pflicht, Tag-/Nacht-Mittelwerte
  des Referenzraums (fuer profilspezifische Anforderungen erforderlich).
- `entity_heat_limit` / `entity_dat` / `entity_dart`: Pflicht, weitere
  anlagenspezifische Kanaele, die an den Server gemeldet werden (fuer
  profilspezifische Anforderungen erforderlich).
- `curve_min`/`curve_max`/`offset_min`/`offset_max`: Sicherheitsgrenzen —
  **muessen** pro Anlage explizit gesetzt werden, es gibt keine sinnvollen
  Standardwerte. Jeder vom Server empfangene und jeder lokal geschriebene
  Boost-Wert wird vor dem Schreiben gegen diese Grenzen geclamped.
- `boost_threshold_k`: Schwelle in Kelvin, ab der die lokale Boost-Hysterese
  eingreift (Ist-Temperatur mehr als `boost_threshold_k` unter der
  Soll-Temperatur). Standard `0.5`.
- `boost_curve_value` / `boost_offset_value`: die Werte, die die lokale
  Boost-Hysterese auf `entity_curve_current`/`entity_offset_current` schreibt,
  solange sie aktiv ist. **Muessen** innerhalb von
  `[curve_min, curve_max]` bzw. `[offset_min, offset_max]` liegen — das
  Add-on verweigert sonst den Start (Fehlermeldung auf stderr, Exit-Code 1),
  statt den Wert stillschweigend zu clampen, da dieser Pfad der einzige ist,
  der ohne Server-Beteiligung auf die Anlage schreibt.
- `poll_interval_seconds`: Intervall zwischen zwei Zyklen (Snapshot
  veroeffentlichen, Boost-Entscheidung treffen). Standard `3600`.
- `notify_service`: optional. HA-Notify-Dienst im Format
  `notify.mobile_app_<geraet>` (z.B. `notify.mobile_app_lucas_iphone`). Ist er
  gesetzt, schickt das Add-on eine Push-Benachrichtigung, sobald ein
  konfigurierter Sensor keinen gueltigen Wert mehr liefert (typischer Fall:
  leere Batterie -> HA meldet `unavailable`). Die Meldung wird in **jedem**
  Zyklus erneut geschickt, solange der Sensor defekt ist — bewusst ohne
  Entprellung, damit ein ausgefallener Sensor nicht untergeht. Leer (Standard)
  = keine Benachrichtigungen; der Ausfall wird dann nur ins Add-on-Log
  geschrieben. Der Versand ist "best effort": schlaegt er fehl, laeuft der
  Zyklus normal weiter.

Ein Sensorausfall betrifft immer nur die betroffene Rolle: deren Wert entfaellt
fuer diesen Zyklus, alle anderen Rollen werden weiterhin gemeldet und die lokale
Boost-Hysterese wird weiterhin ausgewertet.

### `entity_id::attribute`-Konvention

Fuer `climate.*`-Entities ist der HA-`state` der Betriebsmodus
(`heat`/`off`/...), nicht die Temperatur — die eigentlichen Temperaturwerte
stehen in den Attributen `current_temperature`/`temperature`. Jede
`entity_*`-Option kann daher statt einer reinen Entity-ID auch
`entity_id::attribute_name` sein; das Add-on liest dann `attributes.<attribute_name>`
statt `state`. Ohne `::` bleibt das Verhalten unveraendert (liest `state`).

Konkretes Beispiel fuer den Referenzraum:

```yaml
entity_room_actual: "climate.wohnzimmer_thermostat::current_temperature"
entity_room_target: "climate.wohnzimmer_thermostat::temperature"
```

## Verifizierte Architekturen

Aktuell werden nur `aarch64` (Raspberry Pi 4/5, 64-bit — das reale
Deployment-Ziel) und `amd64` (das in den Tests gebaute Ziel) tatsaechlich
gebaut und getestet. `armhf`/`armv7` sind bewusst nicht Teil der `arch`-Liste,
solange sie nicht real gebraucht/getestet werden.
