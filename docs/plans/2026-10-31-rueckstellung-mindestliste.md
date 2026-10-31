# Lokale Mindestliste der Rückstellung (Regel 4, Mittel-Stufe)

Stand: Entwurf, nur rote Tests; kein Produktcode geändert. Änderung braucht die Freigabe des Nutzers (Regel 4).

## Ziel
Welche Hebel beim Abmelden oder Abo-Ende zurückgestellt werden, bestimmt der Client, nicht der Server. Eine
Konfiguration, deren `zurueck` einen Pflicht-Hebel nicht enthält, wird abgewiesen; das vorige Dokument und seine Werte
bleiben (wie bei jedem anderen abgewiesenen Bindungsparameter).

## Befund
- `smartheat_core/bindungsparameter.py`: `beschreibung()` prüft `zurueck` nur mit `_hebel()` (Teilmenge der Hebel des
  Satzes, ohne Doppelte). Leer oder verkürzt wird angenommen; `restore_originals` der `BindingDescription` kommt direkt
  daraus. Ein fehlerhafter Server kann die Rückstellung so auf „Boost beenden“ verkürzen (Gefährdung G-9).
- Heutige Server-Listen (`HeizungssteuerungServer/.../hersteller/bindungen.py` Z. 63–76, nur gelesen): Vaillant
  `heat_limit` (+ `zurueck_optional=heat_limit`), Weishaupt `curve, room_setpoint, heat_limit`, Weishaupt-Basis
  `room_setpoint`, Viessmann `curve, level, room_setpoint`. Die Mindestlisten sind damit genau die heutigen Listen.
- Hebelsatz = `lever_set_id` (`vaillant_vrc720`, `weishaupt_wwp`, `weishaupt_wwp_basis`, `viessmann_vicare`); die
  Hebelnamen stimmen mit `smartheat_core/levers.py` und `LOCAL_SAFETY` überein. Der lokale Teil je Hebelsatz steht in
  `verfahren.py` (`VERFAHREN`), die Sicherheitswerte in `safety.py` (`LOCAL_SAFETY`, Schlüssel Hebelsatz x Verteilsystem).

## Änderung (nach Freigabe)
1. Mindestliste je Hebelsatz als lokale Positivliste. Vorschlag: Feld `rueckstellung_pflicht` in `Verfahren`
   (`verfahren.py`, dort stehen schon die anderen lokalen Positivlisten gegen den Server, Regel-4-Datei). Das ist der
   Hebelsatz-Teil; die Rückstellung hängt nicht am Verteilsystem. Alternative: neben `LOCAL_SAFETY` in `safety.py`
   (Nutzer-Vorgabe); die Wahl entscheidet der Nutzer, siehe Fragen.
2. `bindungsparameter.beschreibung()`: nach `_hebel(zurueck)` Fehler `Fehler(..., "zurueck", "ungueltig")`, wenn die
   Pflichtmenge nicht in `zurueck` liegt. Reihenfolge und zusätzliche erlaubte Hebel egal.
3. Alle Wege laufen über `beschreibung()`: `config_check._pruefen` (Dokument von außen, Abweisung mit Feld/Regel des
   Vertrags) und `dokument_config.beschreibung` (Start, `ConfigError`). Kein zweiter Prüfpfad.
4. Bestehende Tests, die `zurueck=[]` als gültig voraussetzen, werden mit der Änderung angepasst:
   `test_zurueck_optional_nur_aus_zurueck` (Z. 227/228) und der Fall `zurueck_optional` in `_VERSTOESSE` (Z. 412).

## Tests (heute rot, 11 Fälle)
In `heizungsbruecke/tests/test_core_bindungsparameter.py`, Mindestlisten als Literale: je fehlender Pflicht-Hebel
abgewiesen (7 Fälle), `zurueck: []` je Hebelsatz (3), Außenweg über `config_check.pruefen` (Gateway Viessmann, HA Vaillant;
Gegenprobe: heutige Liste wird angenommen). Grün bleiben: volle Liste plus Zusatz-Hebel, andere Reihenfolge, Vaillant
`heat_limit` weiter optional. Rot, weil angenommen statt abgewiesen („DID NOT RAISE“). Außenweg Weishaupt entfällt (die
HA-Rollenprüfung ist dort nicht schmal); AK-G9-06 der Akzeptanzsuite holt den Weg über Server und Gerät nach.

## Abgleich
- S-3 (Anlage schützt sich gegen ihren Server): Mindestliste lokal, nie vom Server. Erfüllt mit der Änderung.
- S-10 (Lieferkette): unberührt. S-11: kein neues Feld im Vertrag; die Abweisung nutzt vorhandene Felder/Regeln.
- Gefährdungsanalyse G-9: schließt die Lücke „Server verkürzt die Rückstellung“; Endzustand der Hebel bleibt der Ursprungswert.

## Rückweg
Reine Prüfverschärfung, keine Schemaänderung: Revert des Commits. Bereits gespeicherte Dokumente tragen die heutigen
Listen und bleiben gültig.

## Offene Fragen an den Nutzer
1. Weishaupt-Basis (`weishaupt_wwp_basis`, Hebel nur `room_setpoint`): Mindestliste `room_setpoint` (wie Server)? Die
   Vorgabe nennt den Satz nicht; ohne Eintrag bliebe er ungeschützt. Empfehlung: aufnehmen.
2. Gespeichert mit kurzer Liste (heute nicht vorgesehen): Start scheitert laut (`ConfigError`, Ruhezustand
   `konfigurationsfehler`, keine Regelung) oder bleibt es bei der Abweisung nur neuer Dokumente? Empfehlung: lautes
   Scheitern, wie die übrigen Bindungsparameter (keine Kompatibilitätsschicht).
3. Vaillant: `heat_limit` ist Pflicht und zugleich `zurueck_optional` (darf im Wiederherstellungspunkt fehlen). So
   belassen? Empfehlung: ja (Hardware-Eigenheit, nicht Server-Willkür).
4. Ort der Liste: `Verfahren` (`verfahren.py`) oder `LOCAL_SAFETY`-Zeile (`safety.py`, Schlüssel mit Verteilsystem)?
   Empfehlung: `Verfahren`; beide sind Regel-4-Dateien mit Wertetabelle in `tests/test_regel4_werte.py`, die um die
   Mindestlisten zu ergänzen ist (Hook-geschützt, braucht den Nutzer).
