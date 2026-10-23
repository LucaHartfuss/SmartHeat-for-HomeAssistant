"""HA-freier Client-Kern von SmartHeat (Hersteller-Abstraktion): Hebel, Hebelsaetze, lokale
Sicherheit, Binding-Schnittstelle, Hebel-Pipeline, Durchsetzen, Mindestvorlauf, Boost, Schreibbudget. Rein: Anlage,
Speicher, Benachrichtigung und Uhr kommen ueber Schnittstellen; importiert nur die Standardbibliothek
(tests/test_modulgrenzen.py). Laeuft im Add-on heizungsbruecke und im SmartHeat-Gateway; ein Umzug in die Integration
ist nicht geplant, aber nicht verbaut (Zielbild)."""
