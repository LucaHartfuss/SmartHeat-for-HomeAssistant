# Heizungsbruecke

Liest konfigurierte Home-Assistant-Entities (Referenzraum, Aussentemperatur und die
Hebel der Anlage: Heizkurve, Parallelverschiebung, Heizgrenze) und meldet sie generisch an den
SmartHeat-Server (Protokoll Schema 4, seit 0.30.0). Schreibt vom Server empfangene Sollwerte zurueck, geclamped
gegen die konfigurierten Sicherheitsgrenzen. **Boost** aktiviert ausschliesslich,
wenn die Wunschtemperatur erhoeht wird (Komfort-Beschleunigung): das Add-on
schaltet kurzzeitig auf eine hohe Heizkurve und Parallelverschiebung, bis der
Raum innerhalb von `boost_threshold_k` (Default 0.5 K) an die neue
Wunschtemperatur herangekommen ist, und schaltet danach zum zuletzt vom Server
empfangenen Wiederherstellungspunkt zurueck. Zu den geschriebenen Werten gehoert seit 0.27.0 auch die
**Heizgrenze** (vom Server gefuehrt, bei Boost 23 Grad): das Add-on merkt sich beim ersten Start den
urspruenglichen Wert der Anlage und stellt ihn bei Abo-Ende oder Abmelden wieder her.
Boost reagiert NICHT auf einen kalten Raum aus anderer Ursache
(Aussentemperatur-Einbruch, offene Tuer) -- diese Faelle werden vom naechsten
regulaeren Heizkurven-Tick abgedeckt. Ist der Server laengere Zeit nicht
erreichbar, uebernimmt stattdessen der separate **Notbetrieb** (siehe
CHANGELOG.md): eine hartcodierte Temperatur-Hysterese, die nur waehrend
einer erkannten Server-Downtime aktiv ist. Dieses Add-on hat keine
eigene Konfigurationsoberflaeche (weder im Configuration-Tab noch als
Ingress-Panel) -- eingerichtet wird es ueber die separate **SmartHeat**
Home-Assistant-Integration: installieren, dann Einstellungen → Geraete &
Dienste → Integration hinzufuegen → "SmartHeat". Die Integration schreibt die
noetige Konfiguration automatisch in dieses Add-on.

## Änderungen

Alle Versionen und Update-Hinweise stehen in [CHANGELOG.md](CHANGELOG.md) (auch im Update-Dialog des Supervisors).

## Voraussetzungen

- Das Add-on **Cloudflared Access TCP-Bridge** (`cloudflared_access_mqtt`, aus
  demselben Repository) muss installiert, konfiguriert und **gestartet** sein,
  bevor dieses Add-on gestartet wird — es stellt den MQTT-Broker unter
  `127.0.0.1:<local_port>` bereit.
- Adresse und Port des Brokers kommen aus dem Deskriptor (Option `transport`); beim
  Mosquitto-Deskriptor ist das der lokale Port von `cloudflared_access_mqtt`.
- Dieses Add-on benoetigt `homeassistant_api: true` (Zugriff auf die
  Home-Assistant-Core-API, um Entity-Zustaende zu lesen/Sollwerte zu setzen)
  und `host_network: true` (um `cloudflared_access_mqtt`s Broker unter
  `127.0.0.1` tatsaechlich erreichen zu koennen) — beides ist in `config.yaml`
  bereits gesetzt, wird hier nur der Vollstaendigkeit halber dokumentiert.
- Vaillant (mypyllant): Die Hersteller-Integration muss ihre Werte **mindestens alle 30 Minuten**
  abfragen (Aktualisierungsintervall ≤ 30 min). SmartHeat wartet nach einem eigenen
  Schreibvorgang 35 Minuten, bevor es eine Abweichung als Eingriff in der App wertet; fragt die
  Integration seltener ab, würden eigene Schreibvorgänge fälschlich als Eingriff erkannt. Für
  Weishaupt und Viessmann richtet sich die Wartezeit nach dem Abfrageintervall der Integration
  (2 × Intervall + 60 s, mindestens 2 bzw. 3 Minuten), siehe „Hersteller und Hebelsätze“.

## Konfiguration

Dieses Add-on hat keine eigene Konfigurationsseite. Installiere und starte es einfach — die
gesamte Einrichtung (Login, Anlagenauswahl, Profil, Entity-Zuordnung) laeuft ueber die separate
**SmartHeat**-Integration (Einstellungen → Geraete & Dienste → Integration hinzufuegen →
"SmartHeat"). Die Integration schreibt die noetigen Werte automatisch in dieses Add-on und
startet es danach selbst neu.

Die Integration schreibt dabei folgende Optionen (nicht von Hand ändern): `transport` (Deskriptor des Servers als
JSON: Art `mosquitto_cloudflared` oder `iot_core`, Adresse, Port), `installation_token` (Token der Anlage für die
Abo-Abfrage), je nach Zugangsart `mqtt_username`/`mqtt_password` oder `tls_certificate`/`tls_private_key` (Zertifikat
und Schlüssel, der Schlüssel entsteht in der Integration). Fehlen `transport` oder `installation_token`, meldet das
Add-on „Konfiguration veraltet“ und bleibt im Ruhezustand: dann die SmartHeat-Einrichtung erneut durchführen.
Passwort, Token und Schlüssel erscheinen nie in Log, Status oder Meldungen.

**Erwartetes Verhalten direkt nach der Installation:** Ein frisch installiertes, noch nicht
konfiguriertes Add-on startet und bleibt im Ruhezustand (es regelt nicht und beendet sich nicht). Sobald
die SmartHeat-Integration die Einrichtung abgeschlossen hat, startet sie das Add-on selbst neu. Watchdog
und „Start beim Booten“ schaltet die Integration für beide Add-ons ein. Beim Entfernen der Integration
wird das Add-on abgemeldet (laufender Boost zurückgesetzt, Meldungen entfernt) und gestoppt. Scheitert
das Zurücksetzen, bleibt es im Ruhezustand laufen, versucht es weiter und meldet die Werte, die sonst von
Hand einzustellen sind.

**Fehlerbilder im Status:** „Notbetrieb“ heißt, der SmartHeat-Server antwortet nicht oder die
Verbindung fehlt seit etwa 15 bis 20 Minuten; die Heizung wird dann bei Bedarf lokal abgesichert. „Datenfehler“
mit der Rolle `datentraeger` heißt, der Datenträger des Home-Assistant-Systems ist voll oder
schreibgeschützt; die Regelung pausiert, die Anlage behält ihre letzten Werte.

## Hersteller und Hebelsätze (ab 0.31.0)

Welche Anlage das Add-on bedient, legt die Option `lever_set` fest (schreibt die SmartHeat-Integration; fehlt sie,
gilt Vaillant). Die Entities je Hebelsatz:

| `lever_set` | Integration | Pflicht-Entities (Optionen) |
| --- | --- | --- |
| `vaillant_vrc720` | mypyllant | `entity_curve_current`, `entity_shift_current` (Zone), `entity_heat_limit`, `entity_min_flow` |
| `weishaupt_wwp` | weishaupt_modbus | `entity_curve_current` (Heizkennlinie), `entity_shift_current` (Raumsolltemperatur Normal), `entity_heat_limit` (Sommer-Winter-Umschaltung), `entity_mode_select` (Betriebsart), `entity_setpoint_comfort`, `entity_setpoint_setback` |
| `weishaupt_wwp_basis` | weishaupt_modbus | wie `weishaupt_wwp`, aber ohne Heizkennlinie und Sommer-Winter-Umschaltung (die Heizkennlinie wird, falls angegeben, nur gelesen) |
| `viessmann_vicare` | vicare | `entity_curve_current` (Neigung), `entity_level_current` (Niveau), `entity_shift_current` (Raumtemperatur „normal“), `entity_mode_select` (Climate-Entity des Heizkreises) |

Dazu immer `entity_room_target`, `entity_outdoor_temp` und die Raumfühler. `poll_interval_seconds` (optional, 10–3600 s)
ist das Abfrageintervall der Hersteller-Integration (Standard Weishaupt 30 s, Viessmann 60 s); daraus folgt die
Wartezeit nach einem eigenen Schreibvorgang (2 × Intervall + 60 s, mindestens 2 bzw. 3 Minuten). Fehlt eine
Pflicht-Entity, ist `lever_set` unbekannt oder liegt `poll_interval_seconds` außerhalb von 10–3600 s (oder ist keine
endliche Zahl), bleibt das Add-on im Zustand „Konfigurationsfehler“. Nur beim Abmelden (Abo-Ende, Entfernen der
Integration) gilt bei einem ungültigen Intervall stattdessen der Standardwert, damit das Zurückstellen nicht daran
scheitert.

Weishaupt schreibt höchstens 10 Werte am Tag in den Regler (Gerätespeicher, laut Weishaupt 100.000 Schreibvorgänge auf
Lebensdauer); weitere Serverwerte werden gespeichert und nach Mitternacht geschrieben. Boost, Notfall-Boost und das
Zurückstellen beim Abo-Ende oder Entfernen sind davon ausgenommen. Das Normal-Soll liegt immer zwischen Absenk- und
Komfort-Soll; SmartHeat verschiebt Komfort bzw. Absenk dafür vorher mit und stellt beide beim Ende zurück. Beim
Zurückstellen wird die Betriebsart (Weishaupt) bzw. das Heizprogramm (Viessmann) nicht vorher umgeschaltet; die
gemerkten Ursprungswerte (Betriebsart bzw. Heizprogramm, Komfort-/Absenk-Soll) werden nach den Hebeln zurückgeschrieben
und danach vergessen. Ist das Weishaupt-Tageslimit erreicht, zählt ein abgebrochener Versuch der Vorbereitung (Betriebsart) nicht als
Fehlversuch; du erhältst den Hinweis zum Tageslimit (einmal am Tag), und der nächste lokale Check versucht es erneut.

Die Optionen der Betriebsart-Select-Entity von weishaupt_modbus sind Übersetzungsschlüssel, nicht Texte (ab Add-on 0.33.0):
„Normal“ heißt in weishaupt_modbus 1.x `hz_operationmode_normal`, ab 2.0 `heating_circuit_operation_mode_normal`; die Heizungsbrücke erkennt die Version am
aktuellen Wert und stellt beim Abmelden die ursprüngliche Betriebsart im passenden Schema zurück. Hat die Betriebsart
einen unbekannten Wert (eine künftige Version benennt die Optionen erneut), stellt die Heizungsbrücke nichts um und
meldet den Fehler, statt eine Option zu raten.

## Statusereignis und Hinweise (ab 0.33.0)

Das Ereignis `smartheat_status` hat Schema 2: `hebelsatz` (ID), `hebel` (Wert je Hebel des Hebelsatzes, auch der
vom Add-on abgeleitete Mindestvorlauf) und `gelernt` (Steigung und Heizgrenze aus der letzten Serverantwort, sonst
`null`) ersetzen die früheren Felder `kurve`, `parallelverschiebung`, `mindestvorlauf` und `heizgrenze`. Das braucht die
Integration ab 0.12.0. Die gelernten Werte stehen auch in `backup.json`. Die Hinweise `schreibbudget` (Tageslimit
erreicht) und `schreibzaehler` (Lebensdauer-Hinweis) lassen sich wie die übrigen Kategorien über `notify_hints_off`
abschalten. Neuer Energiekanal `electrical_total` (Option `entity_energy_electrical_total`, Strom gesamt).

## Verifizierte Architekturen

Aktuell werden nur `aarch64` (Raspberry Pi 4/5, 64-bit — das reale
Deployment-Ziel) und `amd64` (das in den Tests gebaute Ziel) tatsaechlich
gebaut und getestet. `armhf`/`armv7` sind bewusst nicht Teil der `arch`-Liste,
solange sie nicht real gebraucht/getestet werden.

Das Hilfs-Add-on `cloudflared_access_mqtt` ist zusätzlich für `armhf`/`armv7` deklariert.
Für SmartHeat insgesamt ist damit ein 64-Bit-System (`aarch64` oder `amd64`) nötig.
