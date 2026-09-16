# Heizungsbruecke

Liest konfigurierte Home-Assistant-Entities (Referenzraum, Aussentemperatur,
aktuelle Heizkurve/Niveau) und meldet sie generisch an den SmartHeat-Server.
Schreibt vom Server empfangene Sollwerte zurueck, geclamped gegen die
konfigurierten Sicherheitsgrenzen. **Boost** aktiviert ausschliesslich, wenn die
Wunschtemperatur erhoeht wird (Komfort-Beschleunigung): das Add-on schaltet
kurzzeitig auf eine hohe Heizkurve, bis der Raum innerhalb von
`boost_threshold_k` (Default 0.5 K) an die neue Wunschtemperatur herangekommen
ist, und schaltet danach zur zuletzt vom Server empfangenen Heizkurve zurueck.
Boost reagiert NICHT mehr auf einen kalten Raum aus anderer Ursache
(Aussentemperatur-Einbruch, offene Tuer, Server laengere Zeit nicht erreichbar)
-- diese Faelle werden nur noch vom naechsten regulaeren Heizkurven-Tick sowie
vom Fail-Safe-Alarm (rein informativ, siehe `failsafe_stale_after_hours` weiter
unten) abgedeckt. Dieses Add-on hat keine
eigene Konfigurationsoberflaeche (weder im Configuration-Tab noch als
Ingress-Panel) -- eingerichtet wird es ueber die separate **SmartHeat**
Home-Assistant-Integration: installieren, dann Einstellungen → Geraete &
Dienste → Integration hinzufuegen → "SmartHeat". Die Integration schreibt die
noetige Konfiguration automatisch in dieses Add-on.

## Update von 0.1.0 auf 0.2.0 (Breaking Change)

Das Feld `profile` ist neu und **Pflicht** — bestehende Installationen muessen es
nach dem Update einmalig in der Add-on-Konfiguration setzen, sonst startet das
Add-on nicht (`FEHLER: Pflichtfeld 'profile' fehlt in der Add-on-Konfiguration`).
Ebenfalls neu: `entity_room_day_avg`, `entity_room_night_avg`,
`entity_heat_limit`, `entity_dat` und `entity_dart` sind jetzt Pflichtfelder
(vorher optional), und `notify_service` ist als optionales Feld hinzugekommen.

## Update von 0.2.0 auf 0.3.0

`curve_min`, `curve_max`, `offset_min` und `offset_max` sind ab jetzt optional.
Werden sie leer gelassen (bzw. auf `0.0` belassen), verwendet das Add-on die
zum gewaehlten `profile` hinterlegten Server-validierten Standardwerte. Ein
weiterhin gesetzter Wert wirkt wie bisher als expliziter Override und hat
Vorrang. Die `profile`-Auswahl ist ausserdem auf Profile beschraenkt, fuer die
solche Standardwerte bereits hinterlegt sind — `vaillant_gastherme_heizkoerper`
aktuell, weitere folgen mit den jeweils verifizierten Werten.

**Achtung bei bestehenden Installationen:** War eines dieser vier Felder bisher
bewusst explizit auf `0.0` gesetzt (nicht als Platzhalter, sondern als
gewollter Wert), wird das ab dieser Version wie "nicht gesetzt" behandelt und
durch den Profil-Standardwert ersetzt — bitte in diesem Fall die Konfiguration
nach dem Update pruefen.

## Update von 0.3.0 auf 0.4.0

Neu: `failsafe_stale_after_hours` (optional, Standard `4.0`). Das Add-on
veroeffentlicht jetzt einen `binary_sensor` ueber MQTT Discovery
(`heizungsbruecke_<tenant_id>_failsafe`), der aktiv wird, sobald seit mehr als
dieser Anzahl Stunden kein gueltiger Sollwert vom Server mehr angewendet wurde
-- kein `configuration.yaml`-Helper noetig, die Entity entsteht automatisch ueber
Home Assistants MQTT-Integration. Die Rueckschaltung auf Normal erfolgt erst nach
zwei aufeinanderfolgenden gueltigen Nachrichten (Anti-Flatter), unabhaengig von
dieser Schwelle.

## Update von 0.4.0 auf 0.5.0 (Breaking Change)

`mqtt_host`, `mqtt_port`, `curve_min`, `curve_max`, `offset_min`, `offset_max`,
`boost_threshold_k`, `boost_curve_value`, `boost_offset_value`,
`entity_room_day_avg`, `entity_room_night_avg`, `entity_dat` und `entity_dart`
entfallen ersatzlos aus der Konfiguration:

- MQTT-Host/Port sind jetzt fest auf `127.0.0.1:18830` verdrahtet (identisch zum
  bisherigen Standardwert und zu `cloudflared_access_mqtt`s `local_port`).
- Clamp- und Boost-Werte kommen jetzt ausschliesslich aus dem gewaehlten `profile`
  (siehe Konfiguration unten) -- kein Override mehr moeglich.
- DAT, DART sowie Tag-/Nachtmittel der Raumtemperatur werden ab jetzt vom Add-on
  selbst automatisch berechnet (ueber beim ersten Start automatisch angelegte
  Home-Assistant-Helfer: drei `statistics`-Sensoren, zwei `input_number`-Helfer,
  alle mit Praefix `smartheat_<tenant_id>_`) statt vom Kunden manuell angelegte
  Entities zu erwarten.
- `entity_outdoor_temp` ist jetzt **Pflicht** (vorher optional) -- wird fuer die
  automatische DAT-Berechnung gebraucht.

**Achtung bei bestehenden Installationen:** nach dem Update auf 0.5.0 die
Konfiguration einmal pruefen -- die entfallenen Felder werden ignoriert, aber
`entity_outdoor_temp` muss gesetzt sein, sonst startet das Add-on nicht.

## Update von 0.5.0 auf 0.6.0 (Breaking Change)

`tenant_id`, `profile`, `entity_room_actual`, `entity_room_target`,
`entity_curve_current`, `entity_offset_current`, `entity_outdoor_temp`,
`entity_heat_limit`, `poll_interval_seconds`, `notify_service` und
`failsafe_stale_after_hours` entfallen als Supervisor-Configuration-Tab-Felder;
`schema: false` bleibt in `config.yaml` bestehen, damit der Assistent selbst
per `POST /addons/self/options` schreiben kann, ohne von Supervisors
Schema-Validierung abgelehnt zu werden. Die Konfiguration laeuft ab jetzt
ausschliesslich ueber den neuen Einrichtungs-Assistenten: Add-on-Panel
("SmartHeat Einrichtung") oeffnen, mit dem SmartHeat-Account einloggen,
Anlage/Profil/Sensoren im gefuehrten Dialog waehlen, bestaetigen.

**Achtung bei bestehenden Installationen:** nach dem Update auf 0.6.0 sind die
bisherigen Configuration-Tab-Werte wirkungslos -- die Einrichtung muss einmal
ueber den neuen Assistenten wiederholt werden, danach das Add-on manuell neu
starten. `poll_interval_seconds`, `notify_service` und
`failsafe_stale_after_hours` behalten ihre bisherigen Defaults (3600s / leer /
4.0h), wenn der Assistent sie nicht abfragt -- fuer eine Aenderung dieser drei
optionalen Werte vorerst `options.json` auf dem Pi direkt anpassen (kein
UI-Schritt dafuer in dieser Version).

## Update von 0.6.0 auf 0.7.0 (Breaking Change)

Der in 0.6.0 eingefuehrte Ingress-Einrichtungs-Assistent (Add-on-Panel
"SmartHeat Einrichtung") entfaellt vollstaendig -- kein Flask-Server, kein
Ingress-Panel, kein Login-Dialog mehr in diesem Add-on. Das Add-on ist ab
jetzt ein reiner synchroner Hintergrunddienst, der beim Start die vorhandene
`options.json` liest und bei fehlender/unvollstaendiger Konfiguration sauber
mit Exit 0 wieder beendet, statt eine eigene Einrichtungs-UI anzubieten.

Die Einrichtung laeuft stattdessen ueber die neue, separate **SmartHeat**
Home-Assistant-Integration (kein Add-on, sondern eine ueber HACS zu
installierende Integration): HACS-Custom-Repository hinzufuegen, "SmartHeat"
installieren, danach Einstellungen → Geraete & Dienste → Integration
hinzufuegen → "SmartHeat" und den gefuehrten Dialog dort durchlaufen. Die
Integration schreibt Anlage/Profil/Sensoren direkt per Supervisor-API in
dieses Add-on und startet es danach selbst.

**Achtung bei bestehenden Installationen:** Wer noch auf dem 0.6.0-Wizard-Flow
ist, muss vor dem Update auf 0.7.0 zuerst die SmartHeat-Integration ueber HACS
installieren -- der bisherige Assistent (Add-on-Panel) ist nach dem Update
nicht mehr erreichbar. Eine bereits ueber den 0.6.0-Wizard geschriebene
`options.json` bleibt gueltig und wird von 0.7.0 weiterhin gelesen; nur der
Weg, sie zu *erstellen* oder zu *aendern*, aendert sich.

## Voraussetzungen

- Das Add-on **Cloudflared Access TCP-Bridge** (`cloudflared_access_mqtt`, aus
  demselben Repository) muss installiert, konfiguriert und **gestartet** sein,
  bevor dieses Add-on gestartet wird — es stellt den MQTT-Broker unter
  `127.0.0.1:<local_port>` bereit.
- Dieses Add-on ist fest auf `127.0.0.1:18830` verdrahtet (kein Config-Feld
  mehr, siehe Changelog 0.5.0) -- `cloudflared_access_mqtt`s `local_port`
  **muss** deshalb auf dessen Standardwert `18830` bleiben, sonst findet das
  Add-on den Broker nicht.
- Dieses Add-on benoetigt `homeassistant_api: true` (Zugriff auf die
  Home-Assistant-Core-API, um Entity-Zustaende zu lesen/Sollwerte zu setzen)
  und `host_network: true` (um `cloudflared_access_mqtt`s Broker unter
  `127.0.0.1` tatsaechlich erreichen zu koennen) — beides ist in `config.yaml`
  bereits gesetzt, wird hier nur der Vollstaendigkeit halber dokumentiert.

## Konfiguration

Dieses Add-on hat keine eigene Konfigurationsseite. Installiere und starte es einfach — die
gesamte Einrichtung (Login, Anlagenauswahl, Profil, Entity-Zuordnung) laeuft ueber die separate
**SmartHeat**-Integration (Einstellungen → Geraete & Dienste → Integration hinzufuegen →
"SmartHeat"). Die Integration schreibt die noetigen Werte automatisch in dieses Add-on und
startet es danach selbst neu.

**Erwartetes Verhalten direkt nach der Installation:** Ein frisch installiertes, noch nicht
konfiguriertes Add-on startet sichtbar und beendet sich kurz danach wieder von selbst (Exit 0,
kein Absturz) -- das ist beabsichtigt und kein Fehler. Sobald die SmartHeat-Integration die
Einrichtung abgeschlossen hat, startet das Add-on von selbst wieder und bleibt dauerhaft laufen.

## Verifizierte Architekturen

Aktuell werden nur `aarch64` (Raspberry Pi 4/5, 64-bit — das reale
Deployment-Ziel) und `amd64` (das in den Tests gebaute Ziel) tatsaechlich
gebaut und getestet. `armhf`/`armv7` sind bewusst nicht Teil der `arch`-Liste,
solange sie nicht real gebraucht/getestet werden.
