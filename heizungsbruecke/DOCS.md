# Heizungsbruecke

Liest konfigurierte Home-Assistant-Entities (Referenzraum, Aussentemperatur,
aktuelle Heizkurve/Parallelverschiebung) und meldet sie generisch an den
SmartHeat-Server. Schreibt vom Server empfangene Sollwerte zurueck, geclamped
gegen die konfigurierten Sicherheitsgrenzen. **Boost** aktiviert ausschliesslich,
wenn die Wunschtemperatur erhoeht wird (Komfort-Beschleunigung): das Add-on
schaltet kurzzeitig auf eine hohe Heizkurve und Parallelverschiebung, bis der
Raum innerhalb von `boost_threshold_k` (Default 0.5 K) an die neue
Wunschtemperatur herangekommen ist, und schaltet danach zum zuletzt vom Server
empfangenen Wiederherstellungspunkt zurueck.
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
- Dieses Add-on ist fest auf `127.0.0.1:18830` verdrahtet (kein Config-Feld
  mehr, siehe Changelog 0.5.0) -- `cloudflared_access_mqtt`s `local_port`
  **muss** deshalb auf dessen Standardwert `18830` bleiben, sonst findet das
  Add-on den Broker nicht.
- Dieses Add-on benoetigt `homeassistant_api: true` (Zugriff auf die
  Home-Assistant-Core-API, um Entity-Zustaende zu lesen/Sollwerte zu setzen)
  und `host_network: true` (um `cloudflared_access_mqtt`s Broker unter
  `127.0.0.1` tatsaechlich erreichen zu koennen) — beides ist in `config.yaml`
  bereits gesetzt, wird hier nur der Vollstaendigkeit halber dokumentiert.
- Die Hersteller-Integration muss ihre Werte **mindestens alle 30 Minuten** abfragen (bei
  mypyllant: Aktualisierungsintervall ≤ 30 min). SmartHeat wartet nach einem eigenen
  Schreibvorgang 35 Minuten, bevor es eine Abweichung als Eingriff in der App wertet; fragt die
  Integration seltener ab, würden eigene Schreibvorgänge fälschlich als Eingriff erkannt.

## Konfiguration

Dieses Add-on hat keine eigene Konfigurationsseite. Installiere und starte es einfach — die
gesamte Einrichtung (Login, Anlagenauswahl, Profil, Entity-Zuordnung) laeuft ueber die separate
**SmartHeat**-Integration (Einstellungen → Geraete & Dienste → Integration hinzufuegen →
"SmartHeat"). Die Integration schreibt die noetigen Werte automatisch in dieses Add-on und
startet es danach selbst neu.

**Erwartetes Verhalten direkt nach der Installation:** Ein frisch installiertes, noch nicht
konfiguriertes Add-on startet und bleibt im Ruhezustand (es regelt nicht und beendet sich nicht). Sobald
die SmartHeat-Integration die Einrichtung abgeschlossen hat, startet sie das Add-on selbst neu. Watchdog
und „Start beim Booten“ schaltet die Integration für beide Add-ons ein. Beim Entfernen der Integration
wird das Add-on abgemeldet (laufender Boost zurückgesetzt, Meldungen entfernt) und gestoppt. Scheitert
das Zurücksetzen, bleibt es im Ruhezustand laufen, versucht es weiter und meldet die Werte, die sonst von
Hand einzustellen sind.

**Fehlerbilder im Status:** „Notbetrieb“ heißt, der SmartHeat-Server antwortet nicht oder die
Verbindung fehlt seit 15 Minuten; die Heizung wird dann bei Bedarf lokal abgesichert. „Datenfehler“
mit der Rolle `datentraeger` heißt, der Datenträger des Home-Assistant-Systems ist voll oder
schreibgeschützt; die Regelung pausiert, die Anlage behält ihre letzten Werte.

## Verifizierte Architekturen

Aktuell werden nur `aarch64` (Raspberry Pi 4/5, 64-bit — das reale
Deployment-Ziel) und `amd64` (das in den Tests gebaute Ziel) tatsaechlich
gebaut und getestet. `armhf`/`armv7` sind bewusst nicht Teil der `arch`-Liste,
solange sie nicht real gebraucht/getestet werden.

Das Hilfs-Add-on `cloudflared_access_mqtt` ist zusätzlich für `armhf`/`armv7` deklariert.
Für SmartHeat insgesamt ist damit ein 64-Bit-System (`aarch64` oder `amd64`) nötig.
