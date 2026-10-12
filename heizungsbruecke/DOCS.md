# Heizungsbruecke

Ab 1.0.0 ist das Add-on ein **SmartHeat-Gerät** für Home Assistant: Es verbindet die Heizung über die
SmartHeat-Integration mit dem SmartHeat-Dienst. Der Dienst lernt die Heizkurve und schickt die Sollwerte; das Add-on
liest die zugeordneten Entities, schreibt die Sollwerte gegen die lokalen Sicherheitsgrenzen begrenzt zurück und sorgt
lokal für Boost und Notbetrieb.

## Änderungen

Alle Versionen und Update-Hinweise stehen in [CHANGELOG.md](CHANGELOG.md) (auch im Update-Dialog des Supervisors).

## Installation und Einrichtung

1. Das Repository im Add-on-Store hinzufügen, **Heizungsbruecke** installieren und starten. Optionen sind nicht nötig.
2. Beim ersten Start erzeugt das Add-on seine Geräte-Identität (Schlüssel und Zertifikat), meldet sich beim
   SmartHeat-Dienst an und verbindet sich direkt per MQTT mit ihm.
3. Home Assistant meldet daraufhin „SmartHeat gefunden“ (Supervisor-Discovery). Die **SmartHeat**-Integration (HACS)
   übernimmt das Gerät mit dem Übernahme-Code aus der Discovery; danach läuft die Einrichtung (Anlage, Profil,
   Zuordnung der Entities) im Assistenten der Integration bzw. im Portal.
4. Die Konfiguration und die Wunschtemperatur kommen als Soll-Dokumente vom Dienst. Das Add-on prüft eine neue
   Zuordnung vor dem Übernehmen gegen Home Assistant (Entity vorhanden, Domäne erlaubt, Schreibziele nur von der
   Hersteller-Integration) und startet danach selbst neu.

Ein Add-on, das nicht übernommen oder nicht eingerichtet ist, regelt nicht: Es meldet sich beim Dienst, zeigt seinen
Zustand im Ereignis `smartheat_status` und wartet im Ruhezustand. Endzustände (abgemeldet, Abo beendet,
Konfigurationsfehler) sind ebenfalls ein Ruhezustand, kein Beenden.

## Optionen

| Option | Bedeutung |
| --- | --- |
| `bootstrap_url` | Adresse der Accounts-API für die Anmeldung des Geräts (nur `https`). Standard ist der Produktionsdienst; nur für Labor und Tests setzen. |
| `log_level` | `debug`, `info` (Standard), `warning` oder `error`. |

Alle früheren Optionen (`tenant_id`, `entity_*`, Zugangs-, Abo- und Intervall-Optionen) entfallen; die Integration
schreibt keine Optionen mehr.

## Was lokal bleibt

- **Sicherheit:** die Grenzen je Hebelsatz und Verteilsystem gelten im Add-on, auch wenn der Dienst etwas anderes
  schickt.
- **Boost:** nur bei einer Erhöhung der Wunschtemperatur (an der zugeordneten Soll-Entity oder an der
  Wunschtemperatur-Entity der Integration), bis der Raum nahe an der neuen Wunschtemperatur ist.
- **Notbetrieb:** antwortet der Dienst länger nicht, sichert das Add-on die Heizung lokal ab (mit Notfall-Boost bei
  kaltem Raum).
- Eine Änderung der Wunschtemperatur wirkt sofort lokal und geht als Bedienwunsch an den Dienst (offline gepuffert).

## Hersteller-Integrationen

- Vaillant (mypyllant): Die Integration muss ihre Werte **mindestens alle 30 Minuten** abfragen. SmartHeat wartet nach
  einem eigenen Schreibvorgang 35 Minuten, bevor es eine Abweichung als Eingriff in der App wertet.
- Weishaupt und Viessmann: Die Wartezeit nach einem eigenen Schreibvorgang folgt dem Abfrageintervall der Integration
  (2 × Intervall + 60 s, mindestens 2 bzw. 3 Minuten). Weishaupt schreibt höchstens 10 Werte am Tag in den Regler;
  weitere Serverwerte werden nach Mitternacht geschrieben (Boost, Notfall-Boost und Zurückstellen ausgenommen).

## Daten

Alles liegt im Add-on-Datenordner `/data`: unter `device/` Identität, Zertifikat, Übernahme-Code und die
Soll-Dokumente; daneben der anlagengebundene Zustand (Sicherung, Notbetrieb, Abo, Laufzeit-Ordner `runtime/`) und die
Liste der angelegten Hilfssensoren. Schlüssel, Zertifikate und der Übernahme-Code erscheinen nie in Log oder Status.

## Entfernen

Wird die Anlage im Portal oder über die Integration entfernt, meldet sich das Add-on ab: Ein laufender Boost wird
beendet und die zuletzt gelernten Werte bleiben eingestellt. Beim nächsten Start löscht es den anlagengebundenen
Zustand und die angelegten Hilfssensoren, solange keine neue Einrichtung gekommen ist; ist Home Assistant dabei nicht
bereit, holt ein späterer Start das Löschen der Hilfssensoren nach. Die Geräte-Identität bleibt, das Gerät kann neu
eingerichtet werden. Zum endgültigen Entfernen das Add-on deinstallieren.

## Verifizierte Architekturen

Gebaut und getestet werden `aarch64` (Raspberry Pi 4/5, 64-bit) und `amd64`. Für SmartHeat ist ein 64-Bit-System
nötig.
