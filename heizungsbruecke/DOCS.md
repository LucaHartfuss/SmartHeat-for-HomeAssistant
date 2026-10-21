# Heizungsbruecke

Das Add-on ist ein **SmartHeat-Gerät** für Home Assistant: Es verbindet die Heizung über die
SmartHeat-Integration mit dem SmartHeat-Dienst. Der Dienst lernt die Heizkurve und schickt die Sollwerte; das Add-on
liest die zugeordneten Entities, schreibt die Sollwerte gegen die lokalen Sicherheitsgrenzen begrenzt zurück und sorgt
lokal für Boost und Notbetrieb.

## Änderungen

Alle Versionen und Update-Hinweise stehen in [CHANGELOG.md](CHANGELOG.md) (auch im Update-Dialog des Supervisors).

## Installation und Einrichtung

1. Entweder die **SmartHeat**-Integration (HACS) hinzufügen: sie trägt das Repository im Add-on-Store ein, installiert
   und startet **Heizungsbruecke** und führt weiter durch die Einrichtung. Oder das Repository selbst im Add-on-Store
   hinzufügen und **Heizungsbruecke** installieren und starten. Optionen sind nicht nötig.
2. Beim ersten Start erzeugt das Add-on seine Geräte-Identität (Schlüssel und Zertifikat), meldet sich beim
   SmartHeat-Dienst an und verbindet sich direkt per MQTT mit ihm.
3. Home Assistant meldet daraufhin „SmartHeat gefunden“ (Supervisor-Discovery; beim Weg über die Integration wartet
   deren Assistent darauf). Die **SmartHeat**-Integration übernimmt das Gerät mit dem Übernahme-Code aus der Discovery;
   danach läuft die Einrichtung (Anlage, Profil, Zuordnung der Entities) im Assistenten der Integration bzw. im Portal.
4. Die Konfiguration und die Wunschtemperatur kommen als Soll-Dokumente vom Dienst. Das Add-on prüft eine neue
   Zuordnung vor dem Übernehmen gegen Home Assistant (Entity vorhanden, Domäne erlaubt, Schreibziele nur von der
   Hersteller-Integration) und startet danach selbst neu.

Ein Add-on, das nicht übernommen oder nicht eingerichtet ist, regelt nicht: Es meldet sich beim Dienst, zeigt seinen
Zustand im Ereignis `smartheat_status` und wartet im Ruhezustand. Endzustände (abgemeldet, Abo beendet,
Konfigurationsfehler) sind ebenfalls ein Ruhezustand, kein Beenden.

## Optionen

| Option | Bedeutung |
| --- | --- |
| `bootstrap_url` | Adresse der Accounts-API für die Anmeldung des Geräts (nur `https`). Standard ist der Produktionsdienst; nur für Labor und Tests setzen. Ist die Angabe ungültig oder die Optionsdatei unlesbar, meldet sich das Add-on nicht an und wartet im Ruhezustand (es weicht nie auf den Produktionsdienst aus); im Log steht der Grund, nie die Adresse. |
| `log_level` | `debug`, `info` (Standard), `warning` oder `error`. |

Weitere Optionen gibt es nicht; die Integration schreibt keine Optionen.

## Was lokal bleibt

- **Sicherheit:** die Grenzen je Hebelsatz und Verteilsystem gelten im Add-on, auch wenn der Dienst etwas anderes
  schickt.
- **Boost:** nur bei einer Erhöhung der Wunschtemperatur (an der zugeordneten Soll-Entity oder an der
  Wunschtemperatur-Entity der Integration), bis der Raum nahe an der neuen Wunschtemperatur ist, höchstens 4 Stunden
  und nicht länger als drei unlesbare Raumwerte in Folge.
- **Notbetrieb:** antwortet der Dienst länger nicht, sichert das Add-on die Heizung lokal ab (mit Notfall-Boost bei
  kaltem Raum).
- Eine Änderung der Wunschtemperatur wirkt sofort lokal und geht als Bedienwunsch an den Dienst (offline gepuffert).

## Wunschtemperatur und Soll-Entity

Die Regelung führt die Wunschtemperatur im Add-on (Datei `runtime/room_target.json`); die zugeordnete Soll-Entity (ein
Thermostat, `climate.*`) ist Eingang und Anzeige. Der zuletzt gestellte Wert gilt, ob im Portal, am Thermostat in Home
Assistant oder über das Ereignis `smartheat_wunsch`.

- **Am Thermostat gedreht:** Der Wert geht nach 10 s Ruhe einmal als Bedienwunsch an den Dienst (15 bis 25 °C in
  0,5-K-Schritten). Das Add-on schreibt den begrenzten Wert zurück ans Thermostat; die Rückmeldung dieses eigenen
  Schreibens zählt 60 s lang nicht als neue Eingabe.
- **Im Portal geändert:** Das Add-on schreibt den neuen Wert ans Thermostat. Ist das Thermostat aus oder im Frostschutz
  (Soll bis 7 °C), bleibt es unberührt und bekommt den Wert, sobald es wieder heizt.
- **Ereignis `smartheat_wunsch`:** Eine Automation kann ein Ereignis `smartheat_wunsch` mit dem Feld `raum_soll`
  (Zahl oder Text mit Zahl, 5 bis 35 °C) senden; es wirkt wie eine Eingabe am Thermostat und wird an das Thermostat
  angeglichen.
- **Beim Start** gilt die gespeicherte Wunschtemperatur, sonst die Bedienung des Dienstes, sonst der Wert, den das
  Thermostat zeigt (15 bis 25 °C), sonst 20 °C. Hat sich das Thermostat während einer Pause des Add-ons verstellt und
  stimmt der gespeicherte Wert mit der Bedienung des Dienstes überein, wird es beim Start zurückgestellt.
- **Home Assistant nicht erreichbar:** Gelingt das Schreiben ans Thermostat nicht (etwa beim Neustart von Home
  Assistant), merkt sich das Add-on die offene Angleichung und holt sie nach, sobald Home Assistant wieder antwortet;
  ein in der Pause am Thermostat gedrehter Wert verliert dann gegen die neuere Bedienung im Portal. Lehnt Home Assistant
  das Schreiben dauerhaft ab, bleibt die Angleichung offen (Warnung im Log etwa alle 5 Minuten); das Thermostat kann die
  Wunschtemperatur dann nicht mehr führen, die Bedienung im Portal gilt weiter.

## Hersteller-Integrationen

- Vaillant (mypyllant): Die Integration muss ihre Werte **mindestens alle 30 Minuten** abfragen. SmartHeat wartet nach
  einem eigenen Schreibvorgang 35 Minuten, bevor es eine Abweichung als Eingriff in der App wertet.
- Weishaupt und Viessmann: Die Wartezeit nach einem eigenen Schreibvorgang folgt dem Abfrageintervall der Integration
  (2 × Intervall + 60 s, mindestens 2 bzw. 3 Minuten). Weishaupt schreibt höchstens 10 Werte am Tag in den Regler;
  weitere Serverwerte werden nach Mitternacht geschrieben (Boost, Notfall-Boost und Zurückstellen ausgenommen).

## Daten

Alles liegt im Add-on-Datenordner `/data`: unter `device/` Identität, Zertifikat, Übernahme-Code, die Soll-Dokumente und
die Kennung der letzten Discovery-Meldung (`discovery.json`, ohne Code); daneben der anlagengebundene Zustand
(Sicherung, Notbetrieb, Abo, Laufzeit-Ordner `runtime/` mit der Wunschtemperatur) und die Liste der angelegten
Hilfssensoren. Schlüssel, Zertifikate und der Übernahme-Code erscheinen nie in Log oder Status; der Code steht nur in
der Discovery-Meldung an Home Assistant.

## Wenn Home Assistant nicht erreichbar ist

Das Add-on startet vor Home Assistant (`startup: services`) und wartet, bis Home Assistant antwortet. Eine neue
Konfiguration, die in dieser Zeit eintrifft, prüft es erst danach (alle 30 s ein neuer Versuch) und lehnt sie nie
wegen eines Ausfalls ab. Das Status-Ereignis `smartheat_status` und die Discovery-Meldung wiederholt es höchstens alle
60 s. Meldungen der Regelung gehen zusätzlich an den SmartHeat-Dienst und erscheinen im Portal.

## Entfernen

Wird die Anlage im Portal oder über die Integration entfernt, meldet sich das Add-on ab: Ein laufender Boost wird
beendet und die Heizung zurückgegeben. Hebel, die das Add-on verstellt hat, gehen auf ihren ursprünglichen Wert zurück,
soweit er bekannt ist: bei Vaillant die Heizgrenze (Heizkurve und Parallelverschiebung bleiben auf dem zuletzt gelernten
Wert), bei Weishaupt Heizkennlinie, Raumsolltemperatur Normal und Sommer-Winter-Umschaltung samt Betriebsart sowie
Komfort- und Absenk-Sollwert, bei Viessmann Neigung, Niveau, Raumtemperatur Normal und das Heizprogramm. Gelingt das
Zurückstellen nicht, versucht das Add-on es erneut und meldet es mit den noch eingestellten Werten. Beim nächsten Start
löscht es den anlagengebundenen Zustand und die angelegten Hilfssensoren, solange keine neue Einrichtung gekommen ist;
ist Home Assistant dabei nicht bereit, holt ein späterer Start das Löschen der Hilfssensoren nach. Die Geräte-Identität
bleibt, das Gerät kann neu eingerichtet werden. Zum endgültigen Entfernen das Add-on deinstallieren.

Auch das Löschen des Eintrags „SmartHeat“ in Home Assistant meldet das Gerät ab, genau wie das Entfernen im Portal
(die Integration schickt dem Add-on dazu den lokalen Befehl; er wirkt nur für die eigene Geräte-ID). Aus dem Konto
entfernt es erst das Portal. Wurde der Eintrag versehentlich gelöscht, den Eintrag neu einrichten und danach
„Neu konfigurieren → Zuordnung ändern“ wählen; sonst bleibt das Gerät „abgemeldet“.

## Verifizierte Architekturen

Gebaut und getestet werden `aarch64` (Raspberry Pi 4/5, 64-bit) und `amd64`. Für SmartHeat ist ein 64-Bit-System
nötig.
