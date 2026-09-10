# Heizungsbruecke

Liest konfigurierte Home-Assistant-Entities (Referenzraum, Aussentemperatur,
aktuelle Heizkurve/Niveau) und meldet sie generisch an den SmartHeat-Server.
Schreibt vom Server empfangene Sollwerte zurueck, geclamped gegen die
konfigurierten Sicherheitsgrenzen. Enthaelt eine lokale Boost-Hysterese,
die unabhaengig vom Server eingreift, wenn der Referenzraum mehr als
`boost_threshold_k` unter der Zieltemperatur liegt.

## Konfiguration

- `tenant_id`: eindeutige Kennung dieser Anlage (bestimmt das MQTT-Topic-Praefix).
- `entity_room_actual` / `entity_room_target`: Ist-/Soll-Temperatur des Referenzraums (Pflicht).
- `entity_curve_current` / `entity_offset_current`: Ziel-Entities fuer Heizkurve/Niveau (Pflicht).
- `entity_outdoor_temp` / `entity_room_day_avg` / `entity_room_night_avg`: optional, je nach Anlage.
- `curve_min`/`curve_max`/`offset_min`/`offset_max`: Sicherheitsgrenzen — **muessen** pro Anlage explizit gesetzt werden, es gibt keine sinnvollen Standardwerte.
