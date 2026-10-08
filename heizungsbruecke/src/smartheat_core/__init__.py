"""HA-freier Client-Kern von SmartHeat (Hersteller-Abstraktion, Spec 5.1, Plan 2 P2-1): Hebel, Hebelsaetze, lokale
Sicherheit, Binding-Schnittstelle, Hebel-Pipeline, Durchsetzen, Mindestvorlauf, Boost, Schreibbudget. Rein: Anlage,
Speicher, Benachrichtigung und Uhr kommen ueber Schnittstellen; importiert nur die Standardbibliothek
(tests/test_core_purity.py). Laeuft im Add-on heizungsbruecke und im SmartHeat-Gateway; ein Umzug in die Integration
ist nicht geplant, aber nicht verbaut (Zielbild 2026-10-07)."""
