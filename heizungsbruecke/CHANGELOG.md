# Changelog

Wird im Update-Dialog des Supervisors angezeigt. Pro Version ein Abschnitt `## X.Y.Z`; der
Release-Workflow übernimmt den Abschnitt der releasten Version in das GitHub-Release.

## Unveröffentlicht

- **Aufräumen ohne Verhaltensänderung (Audit 4 P-E):** Die Übernahme alter `backup.json`-Felder aus Add-on 0.29.0
  entfällt; alle Anlagen laufen seit Release 2 auf neueren Versionen. Doppelte Meldungstexte und veraltete Kommentare
  sind bereinigt. Kein „Neu konfigurieren" nötig.

## 0.35.0

- **Kein „Neu konfigurieren“ nötig:** Das Optionsschema ist unverändert. Die Integration ab 0.12.0 genügt; für `client1`
  (Vaillant) ändern sich im Normalbetrieb Werte, Meldungen und Statusereignis nicht; der Boost bekommt nur die neue
  Höchstdauer (siehe unten). In `backup.json` kommen beim ersten Start nur die
  Kennungen von Einrichtung und Anlage dazu (siehe unten), nichts wird verworfen.
- **Comfort-Boost endet von selbst:** Der Boost nach einer Erhöhung der Wunschtemperatur läuft höchstens 4 Stunden und
  endet früher, wenn der Raumfühler dreimal in Folge nicht lesbar ist; dann kehrt das Add-on auf die gemerkten Werte
  zurück. Ein einzelner oder zweimal unlesbarer Wert beendet den Boost nicht, ein lesbarer Wert setzt die Zählung zurück.
  Ein beim Update bereits laufender Comfort-Boost wird ab dem ersten Check nach dem Update für höchstens 4 Stunden
  weitergeführt. Der Notfall-Boost bei kaltem Raum bleibt unverändert.
- **Konfigurationsfehler nimmt einen laufenden Boost zurück:** Stellt das Add-on beim Start einen Konfigurationsfehler
  fest, während ein Boost läuft, stellt es die gemerkten Werte wieder her. Gelingt das nicht, sagt die Meldung ehrlich,
  dass die Boost-Werte noch stehen und welche Werte zurückzusetzen sind. Ohne laufenden Boost bleibt alles wie bisher
  („behält ihre letzten Werte“).
- **Gemerkter Zustand gehört zu Einrichtung und Anlage:** Die gemerkten Ursprungswerte, der Wiederherstellungspunkt und
  der übrige Zustand in `backup.json` tragen jetzt die Kennung der Einrichtung und der Anlage. Wer eine andere Anlage neu
  einrichtet oder später die Hebel-Zuordnung (die zugeordneten Hebel-Entities) ändert, gilt als andere Anlage und
  startet sauber; nach einem erfolgreichen Start werden gemerkte Ursprungswerte nie auf eine
  fremde Anlage geschrieben. Bestehende Installationen (z. B. `client1`) behalten ihren Zustand: Beim ersten Start werden
  nur Einrichtungs- und Anlagenkennung ergänzt.

## 0.34.0

- **Kein „Neu konfigurieren“ nötig:** Das Optionsschema ist unverändert. Die Integration ab 0.12.0 genügt; für `client1`
  (Vaillant) ändern sich Werte, Boost, Meldungen, Statusereignis und `backup.json` nicht.
- **Wertebereich der Anlage:** Geschriebene Werte werden zusätzlich auf den Wertebereich der zugeordneten Entity begrenzt
  (`min`/`max` der Zahl-Entity bzw. `min_temp`/`max_temp` der Klima-Entity), nie weiter als die lokalen Sicherheitsgrenzen
  des Add-ons – der Bereich kann sie nur verengen. Fehlt der Anlagenbereich, ist er unbrauchbar oder schneidet er sich
  nicht mit den lokalen Grenzen, gelten die lokalen Grenzen. Der Bereich wird je Hebel fünf Minuten gemerkt, bei einem
  Lesefehler bleibt der letzte gute Wert; weicht er erstmals vom lokalen ab, steht eine Zeile im Log. Auch das Durchsetzen
  des Mindestvorlaufs rechnet mit diesem Bereich (sonst bliebe eine dauerhafte Abweichung). Nicht zugeordnete Hebel
  erzeugen keinen Traceback mehr.
- **weishaupt_modbus 2.0 neben 1.x:** Die Betriebsart „Normal“ heißt in 1.x `hz_operationmode_normal`, ab 2.0
  `heating_circuit_operation_mode_normal`; das Add-on erkennt das Schema am aktuellen Wert, wählt beim Vorbereiten „Normal“
  und stellt beim Abmelden den Ursprungswert im passenden Schema zurück. Bei einem unbekannten Schema stellt es nichts um
  und meldet den Fehler, statt eine Option zu raten. Der Normal-Bereich des Raumsolls ist je Schema fest (1.x 16–28 °C,
  2.0 18–25 °C).
- **Kern-Extraktion (SHG G1/G2a, verhaltensgleich):** Laufzeit, Abo-Prüfung, Meldungen und Regelpipeline liegen jetzt in den
  Paketen `smartheat_runtime` und `smartheat_core`, die ohne Home Assistant laufen können (Grundlage des SmartHeat-Gateways);
  das Add-on ist nur noch die dünne Home-Assistant-Anbindung (Host, Sinks, Texte). Ein Golden-Master-Test des ganzen Add-ons
  vor der Umstellung belegt, dass Schreibwerte, Statusereignisse und Meldungen unverändert bleiben. Die Kundentexte bleiben
  unverändert.

## 0.33.0

- **Hersteller-Abstraktion (Plan 3c):** Das Add-on-Schema kennt jetzt die Optionen der Weishaupt- und Viessmann-Hebelsätze
  (`lever_set`, `poll_interval_seconds`, `entity_level_current`, `entity_mode_select`, `entity_setpoint_comfort`,
  `entity_setpoint_setback`, `entity_energy_electrical_total`); `entity_curve_current` und `entity_heat_limit` sind
  optional (Weishaupt-Basis hat keine Steigung, Viessmann keine Heizgrenze), die Pflichtprüfung je Hebelsatz bleibt.
- **Fix Weishaupt:** Die Betriebsart „Normal“ wird als Übersetzungsschlüssel `hz_operationmode_normal` gelesen und
  geschrieben (weishaupt_modbus 1.0.20 liefert für Select-Entities die Schlüssel statt der Texte); vorher hätte das
  Add-on die Betriebsart nie umgestellt.
- **Statusereignis Schema 2:** `hebelsatz` (ID), `hebel` (alle Hebel des Hebelsatzes samt abgeleitetem `min_flow`) und
  `gelernt` (`curve`, `heat_limit` aus der letzten Serverantwort, sonst `null`) ersetzen `kurve`, `parallelverschiebung`,
  `mindestvorlauf` und `heizgrenze`; der Hinweis `manueller_eingriff` trägt `{hebel, erkannt}`.
- Gelernte Werte der letzten Serverantwort stehen in `backup.json` (`learned`, wie der Wiederherstellungspunkt).
- Hinweis-Kategorien `schreibbudget` und `schreibzaehler` lassen sich über `notify_hints_off` abschalten.
- Neuer Energiekanal `electrical_total` (Strom gesamt).
- **Integration ab 0.12.0 nötig** (Statusereignis Schema 2): Add-on und Integration innerhalb von 15 Minuten
  nacheinander aktualisieren, sonst startet der Wächter der älteren Integration das Add-on neu. Danach in der Integration
  „Neu konfigurieren“ (ein Eintrag ohne Hebelsatz gilt dort als unvollständig).
- Für `client1` (Vaillant) ändern sich Werte, Boost und Meldungen nicht; nur das Statusereignis hat neue Felder.

## 0.32.0

- **Neu einrichten nötig:** Eine Konfiguration von vor dieser Version meldet „Konfiguration veraltet“ – die
  SmartHeat-Einrichtung (Integration) einmal erneut durchführen. Es gibt keinen stillen Rückfall auf die alten Optionen.
- Transport-Deskriptor (`transport`) und Installations-Token (`installation_token`) statt fester Adresse
  (`127.0.0.1:18830`) und MQTT-Passwort für die Abo-Abfrage `/status`; das Token geht als Bearer-Token an den
  Server und taucht in keinem Log oder Status auf.
- Zertifikats-Anmeldung (AWS IoT Core) vorbereitet, inaktiv (`tls_certificate`, `tls_private_key`).
- TLS-, Anmelde- und Netzwerkfehler sind im Log unterscheidbar; der Hinweis auf `cloudflared_access_mqtt` erscheint
  nur noch beim Mosquitto-Transport.
- Abo-Erkennung auch ohne CONNACK: fehlt die Verbindung seit 15 Minuten und sind mindestens 3 Versuche in Folge
  gescheitert (auch Trennungen vor dem CONNACK zählen), fragt das Add-on den Abo-Status (höchstens alle 10 Minuten).
  „Inaktiv“ wechselt in den Abo-inaktiv-Modus, „abgelehnt“ meldet wie eine abgelehnte Anmeldung.
- Eigenes Icon und Logo (SmartHeat-Flamme) im Add-on-Store und in der Add-on-Übersicht.

## 0.31.0

- **Hersteller-Abstraktion (Plan 3b):** Der Kern kann neben Vaillant (`vaillant_vrc720`) auch Weishaupt-Wärmepumpen
  über `weishaupt_modbus` (`weishaupt_wwp`, Rückfall `weishaupt_wwp_basis` nur mit dem Raumsoll) und Viessmann über
  `vicare` (`viessmann_vicare`: Neigung, Niveau, Raumtemperatur „normal“) bedienen. Die zugehörigen Profile des
  Servers sind noch inaktiv (Inventur ausstehend); eingerichtet werden sie ab dem nächsten Integrations-Release.
- Lokale Sicherheitswerte (Regel 4) für die neuen Hersteller und für Fußbodenheizung (alle Hebel am Maximum ergeben
  bei −15 °C höchstens 45 °C Vorlauf; kein Komfort-Boost bei Fußbodenheizung).
- Weishaupt: höchstens 10 Schreibvorgänge am Tag (Gerätespeicher), Hinweis ab 50.000 Schreibvorgängen; Betriebsart,
  Komfort- und Absenk-Soll werden gemerkt und bei Abo-Ende oder Entfernen zurückgestellt. Viessmann: Neigung und
  Niveau zählen als ein Schreibvorgang, höchstens 4 Korrekturen je Tag.
- Tageszähler für Energie werden als fortlaufende Summe gemeldet.
- Für bestehende Vaillant-Installationen ändert sich nichts (Werte, Boost, Meldungen, `backup.json`).

## 0.30.0

- **Hersteller-Abstraktion (Plan 2):** Protokoll Schema 4 – der Snapshot meldet `room_target` und die Hebel des
  Hebelsatzes (`levers`, `readonly`), die Antwort trägt `levers` und `learned`. Braucht einen Server mit Schema 4.
- Neuer HA-freier Kern `smartheat_core` (Hebel, Hebelsätze, lokale Sicherheit, Binding, Hebel-Pipeline, Durchsetzen,
  Mindestvorlauf, Boost, Schreibbudget); mypyllant-Spezifisches im HA-Binding `ha_binding.py`.
- `backup.json` wird beim ersten Start einmalig auf Hebelnamen umgestellt (Wiederherstellungspunkt, Ursprungswerte,
  Schreibbudget, Eingriff). Rückweg auf 0.29.0: startet ohne Wiederherstellungspunkt wie eine Neuinstallation.
- Verhalten für Vaillant unverändert (Sicherheitswerte, Boost, Durchsetzen, Kundenmeldungen). Einzige sichtbare
  Änderung: Datenfehler-Meldungen nennen jetzt Hebel statt Rollen (`curve` statt `curve_current`, `room_setpoint`
  statt `shift_current`), im Meldungstext und im Status-Event-Attribut `rollen`; ein von 0.29.0 gespeicherter
  Datenfehler wird nach dem Update einmal erneut gemeldet.

## 0.29.0

**Heizgrenze bis 23 °C.**

- Die lokale Obergrenze der Heizgrenze steigt für Heizkörper von 20 auf 23 °C (Regelkern 3.0 des Servers, TP13).
  Der Server führt die Heizgrenze künftig als gelernte Stellgröße zwischen 10 °C und Raum-Soll − 0,5 K.
- Folge: Comfort- und Notfall-Boost setzen die Heizgrenze auf 23 °C statt 20 °C.
- Kein Fehlalarm „Anlage folgt nicht“ mehr nach einer Soll-Änderung kurz vor dem Tagestick: In den 35 Minuten nach
  einem eigenen Schreibvorgang meldet das Add-on jetzt auch für Steigung und Heizgrenze den geschriebenen Wert,
  nicht den veralteten Stand aus der Hersteller-Cloud (bisher nur für die Parallelverschiebung).
- Verträglich nur mit einem TP12h-fähigen Server (Antwortfeld `heat_limit`; ab Add-on 0.27.0 nötig). Ist der
  Live-Server bereits TP12h-fähig, dieses Add-on vor dem Server-Update mit TP13 einspielen. Sonst (gebündeltes
  Release) zuerst den Server, das Add-on unmittelbar danach (Runbook „TP13-Deploy“, Fall A/B).

## 0.28.0

**Verlässlichere Installation und ein engeres Intervall.**

- Das Telemetrie-Intervall darf höchstens noch 10 Minuten (600 s) betragen, vorher 15 Minuten. Der Server wertet
  Lücken ab 15 Minuten als Pause; bei 15 Minuten Intervall blieb dafür keine Reserve, und die Regelung hätte
  nie gelernt. Der Standard von 5 Minuten bleibt. Ein in den Optionen gespeicherter Wert über 600 s wird vom
  Supervisor abgelehnt (das Add-on startet dann nicht); bitte vor dem Update anpassen.
- Die Installation des Add-ons nutzt feste Paketstände mit Prüfsummen und ein festes Basis-Image, dadurch ist
  jede Installation identisch getestet.
- Moduswechsel der Heizzone: Hat das Add-on die Zone gerade selbst auf „Manuell“ gestellt, schaltet eine
  Server-Antwort in den folgenden 35 Minuten nicht ein zweites Mal um (Home Assistant zeigt den neuen
  Modus bis zum nächsten Abruf der Hersteller-Cloud noch nicht). Das spart Zugriffe auf die Hersteller-Cloud.
  Danach wird eine zurückgestellte Zone wie bisher wieder korrigiert.

## 0.27.0

- Heizgrenze als Stellgröße (TP12h): Der Server führt die Heizgrenze der Anlage und hebt sie bei Abschaltung durch
  die Heizgrenze und zu kaltem Raum um bis zu 4 K (höchstens 20 °C) an; das Add-on schreibt sie wie Steigung und
  Parallelverschiebung, setzt sie bei Boost auf 20 °C, stellt sie nach Verstellen in der App zurück und merkt
  sich beim ersten Start den Ursprungswert, auf den sie bei Abo-Ende oder Abmelden zurückgeht.
- Neue lokale Grenzen: Heizgrenze 5 bis 20 °C (Heizkörper).
- Voraussetzung: SmartHeat-Server mit TP12h (Antwortfeld `heat_limit`); eine Antwort ohne das Feld gilt als ungültig.
- Status-Event: neues Feld `heizgrenze`. Die Heizgrenze muss eine `number`-Entity sein (Neu konfigurieren, wenn
  bisher ein Sensor gewählt war).

## 0.26.0

**SmartHeat merkt, wenn die Therme keine Heizwärme liefert.**

- Fordert die Heizung Wärme an, die Therme liefert aber über Stunden keine (zum Beispiel weil sie im
  Sommerbetrieb steht, Statuscode S.031), meldet SmartHeat das jetzt: als Hinweis auf dem Handy, im
  Status-Sensor (Attribut `waerme_fehlt`) und beim Betreiber. Die Heizkurvenoptimierung pausiert dann,
  damit sie nicht aus einem ungeheizten Haus lernt, und läuft von selbst weiter, sobald wieder Wärme ankommt.
- Eine einmal erkannte Situation wird erst aufgehoben, wenn die Wärme über eine Stunde gemittelt (Median)
  ankommt. Restwärme nach einer Warmwasserladung löst die Entwarnung nicht aus, ein taktender, aber
  heizender Brenner hebt sie nach spätestens etwa zwei Stunden auf.
- Der Hinweis lässt sich in den SmartHeat-Optionen abschalten („Benachrichtigen, wenn die Therme keine
  Heizwärme liefert“). Voraussetzung sind die Sensoren „Vorlauftemperatur“ und „Vorlauf-Soll“ im Mapping.
- SmartHeat schreibt dafür nichts an die Anlage; Steigung, Parallelverschiebung und Mindestvorlauf bleiben
  unverändert.

## 0.25.0

**Fehler werden gemeldet, die Hersteller-Cloud wird geschont.**

- Ist der Datenträger des Home-Assistant-Systems voll oder schreibgeschützt, meldet SmartHeat das
  jetzt (Status „Datenfehler“, Push) statt still stehen zu bleiben. Die Meldung verschwindet von
  selbst, sobald wieder gespeichert werden kann.
- Fehlt die Verbindung zum SmartHeat-Server etwa 15 bis 20 Minuten lang, geht das Add-on in den Notbetrieb und
  meldet das – auch direkt nach dem Start und wenn gerade keine Anpassung anstand. Der Notbetrieb
  endet, sobald der Server wieder antwortet.
- Ist das Raumthermostat beim Start nicht lesbar, wird das als Datenfehler gemeldet.
- Nach einer Abo-Reaktivierung endet ein übrig gebliebener Notbetrieb sofort.
- Scheitert das Schreiben an die Anlage (z. B. Abrufgrenze der Hersteller-Cloud erreicht), versucht
  SmartHeat es gestaffelt erneut statt bei jeder Raumtemperatur-Änderung. Das Zurückstellen der
  Anlage (Ende eines Boosts, Wiederherstellung) wiederholt es nach 5, 15 und 30 Minuten, danach
  halbstündlich. Ein Boost-Start und die Vorbereitung der Zone werden frühestens nach 30 Minuten
  wiederholt, höchstens 6 gescheiterte Versuche am Tag. Nur die Tageszählung übersteht einen
  Neustart, der Mindestabstand beginnt dann neu.
- Nicht verfügbare Anlagenwerte werden nicht mehr „geschrieben“; heizt die Zone gerade nicht, wird
  die Wunschtemperatur nicht bei jeder Serverantwort neu übertragen.
- Umbenannte alte Hilfssensoren (DAT, 24-h-Minimum) werden jetzt ebenfalls entfernt.

## 0.24.0

**Neue Regelung (TP11): Steigung und Parallelverschiebung.** Die Parallelverschiebung der Heizkurve
ist jetzt die Wunschtemperatur der Heizzone (bei Vaillant im Modus „Manuell“) – die
Mindestvorlauftemperatur ist nur eine Untergrenze und folgt ab jetzt automatisch deiner
Wunschtemperatur am Raumthermostat.

- Beim Start stellt SmartHeat die Heizzone auf „Manuell“. Damit entfällt eine Nachtabsenkung
  aus dem Zeitprogramm der Therme; das Zeitprogramm bleibt in der Therme gespeichert.
- Einstellungen an Heizkurve, Wunschtemperatur der Zone, Mindestvorlauf oder Betriebsart in der
  Hersteller-App setzt SmartHeat zurück (einmalige Meldung). Bitte nur noch am Raumthermostat
  einstellen.
- Der Boost hebt jetzt Heizkurve und Parallelverschiebung an und wirkt damit deutlich stärker.
- Die Hilfssensoren DAT, DART, Raum-Mittel, 24-h-Minimum und Tag-/Nachtmittel werden entfernt;
  der Server rechnet aus der Telemetrie.
- **Nach dem Update SmartHeat einmal „Neu konfigurieren“** (Parallelverschiebung,
  Mindestvorlauftemperatur und Vorlauf-Soll zuordnen). Bis dahin meldet das Add-on
  „Konfiguration veraltet“ und verändert nichts an der Heizung.
- Beim ersten Start mit der neuen Konfiguration übernimmt SmartHeat die aktuell an der Therme
  eingestellte Heizkurve als Ausgangswert und meldet sich sofort beim Server, statt bis zum
  nächsten Tagestick zu warten.

## 0.23.0

- Eine Server-Antwort mit unbekanntem Schema wird als Datenfehler vom Server gemeldet und nicht übernommen.
- Status: Notbetrieb und Datenfehler werden auch vor der ersten MQTT-Verbindung angezeigt statt „startet“.
- Entfernen der Integration: Scheitert das Zurücksetzen eines laufenden Boosts, kommt eine kritische
  Meldung mit den Werten zum Einstellen von Hand; das Add-on versucht es weiter, auch ohne
  Zugangsdaten und nach einem Neustart, und meldet die Entwarnung.
- Aufräumarbeiten ohne Verhaltensänderung: ungenutzter Code entfernt, Warnung bei einer beschädigten `failsafe_state.json`, zusätzliche Tests.

## 0.22.0

- Die Telemetrie meldet einen anstehenden Datenfehler (Quelle und Details) an den Server, damit
  dessen Health-Check auch Schreibfehler zur Anlage sieht. Keine Verhaltensänderung an der Regelung.

## 0.21.0

- `boot: auto` aus der Add-on-Konfiguration entfernt (Standardwert, keine Verhaltensänderung).
- Die Abo-Status-Abfrage meldet sich mit den MQTT-Zugangsdaten der Anlage an (setzt den
  Server-Stand TP8 voraus). Bei dauerhaft abgelehnter Anmeldung fragt das Add-on höchstens alle
  10 Minuten nach und schreibt die Fehlerzeile nur einmal.

## 0.20.0

Nötig: SmartHeat-Integration ab Version 0.6.0. Nach dem Update meldet das Add-on bis zum
„Neu konfigurieren“ in der Integration weiter „Konfiguration veraltet“; die Anlage behält ihre Werte.

Neu: Status in Home Assistant. Die Integration zeigt am Gerät „SmartHeat <Anlage>“ Status, Notbetrieb,
Datenfehler (mit Quelle), Boost, letzte Serverantwort, zuletzt gelernte Kurve/Offset, Abo und
Add-on-Version. Das Add-on meldet sich dafür alle 5 Minuten; bleibt das aus oder ist ein Add-on
gestoppt, meldet die Integration das selbst und startet es neu.

Neu: Das Add-on beendet sich nie mehr von selbst. Nicht eingerichtet, Konfigurationsfehler, Abo beendet
und abgemeldet sind ein Ruhezustand ohne Regelung. Bei einem Konfigurationsfehler prüft es nach 15 Minuten
erneut, ohne die Meldung zu wiederholen. Watchdog und Start beim Booten setzt die Integration.

Neu: Manuelle Änderungen an Heizkurve oder Offset werden erkannt und gemeldet. SmartHeat setzt sie beim
nächsten Regelschritt weiterhin zurück. Gemeldet wird erst, wenn die Abweichung zwei Prüfungen in Folge
besteht, und nie in den ersten 35 Minuten nach einem eigenen Schreiben von SmartHeat auf Kurve/Offset
(neue Serverwerte, Boost-Start/-Ende): die myVAILLANT-Integration zeigt einen geschriebenen Wert unter
Umständen erst mit ihrer nächsten Abfrage (alle 30 Minuten). Eine Änderung von Hand kurz nach einem
eigenen Schreiben wird deshalb bis zu 35 Minuten später erkannt.

Neu: Hinweise (ausgefallener Raumfühler, schwache Batterie, manueller Eingriff, Quellwechsel) lassen sich
in den Optionen der Integration einzeln abschalten. Kritische Meldungen bleiben immer an.

Neu: Scheitert am Ende der Abo-Frist das Zurücksetzen auf die gelernten Werte, kommt eine Meldung.
Lehnt der Server die Zugangsdaten ab, zeigt der Status das und Home Assistant fordert zur neuen Anmeldung auf.

Geändert: Ein Comfort-Boost läuft über einen Neustart weiter und endet regulär; ein nach dem Neustart nicht
lesbarer Raumfühler lässt die Boost-Flags stehen, der nächste Check beendet den Boost mit Zurücksetzen.
Ein Notfall-Boost startet auch, wenn nur ein älterer Wiederherstellungspunkt gespeichert werden konnte.
Beim Verbinden mit dem Broker wird ein offener Tick nur noch sofort wiederholt, wenn er auf den Server
wartet oder gar nicht gesendet war.

Geändert: Ein HTTP 404 der Abo-Abfrage zählt nur noch mit einer Antwort der SmartHeat-accounts-api als
„Abo inaktiv“.

Entfällt: MQTT-Discovery (`binary_sensor` Fail-Safe), `status/failsafe` und das Last Will auf
`status/availability`; die Status-Entity per `POST /api/states`.

## 0.19.0

Nötig: SmartHeat-Integration ab Version 0.5.0. Nach dem Update meldet das Add-on
„Konfiguration veraltet – bitte SmartHeat-Einrichtung erneut durchführen“ (Status-Entity,
Benachrichtigung in Home Assistant) und regelt nicht, bis die Einrichtung neu durchlaufen ist.
Die Anlage behält bis dahin ihre letzten Werte. Dazu den bestehenden SmartHeat-Eintrag unter
Einstellungen → Geräte & Dienste entfernen und die Integration neu hinzufügen.

Neu: Mehrere Raumfühler. Die Raumtemperatur ist der Mittelwert aller Fühler mit gültigem Wert
(5–35 °C), als eigener Sensor „SmartHeat <Anlage> Raumtemperatur“. Fällt ein Fühler aus, rechnet
das Add-on mit den übrigen weiter und meldet das, sobald er bei zwei Prüfungen hintereinander
(Abstand 5 Minuten) keinen gültigen Wert liefert; kurze Aussetzer bleiben still. Thermostate
(`climate`) sind als Raumfühler möglich.

Neu: Außentemperatur auch aus einem Wetterdienst (`weather`), wenn die Heizung keinen
Außenfühler hat („SmartHeat <Anlage> Außentemperatur“).

Neu: Meldungen an mehrere Handys. Alles, was die Regelung stoppt (Notbetrieb, Datenfehler,
Abo inaktiv, Konfigurationsfehler), erscheint zusätzlich als Benachrichtigung in Home Assistant,
bis es behoben ist, auch über einen Neustart von Home Assistant hinweg. Jede Push-Meldung kommt nur
einmal, auch nach einem Neustart.

Neu: Batterieüberwachung der Raumfühler und Thermostate (Meldung unter 20 %, Entwarnung ab 25 %).

Neu: Status-Entity `sensor.smartheat_<anlage>_status` (`startet`, `bereit`,
`konfigurationsfehler` mit Grund). Die Integration wartet bei der Einrichtung darauf.

Geändert: Fehlt eine Pflicht-Entity oder lässt sich ein Hilfssensor nicht anlegen, meldet das
Add-on nach etwa 4 Minuten einen Konfigurationsfehler und beendet sich. Solange Home Assistant
selbst noch nicht erreichbar oder noch nicht fertig gestartet ist (z. B. eine Cloud-Integration lädt
nach einem Neustart noch), wartet es weiter; die 4 Minuten zählen erst danach.

Einmalig nach dem Update: Die Hilfssensoren DAT, DART, das Raum-Mittel und das 24-h-Minimum
werden neu angelegt (gleiche Namen und Entity-IDs). DART und das Raum-Mittel beginnen leer, weil
sie jetzt auf dem neuen Raumtemperatur-Sensor beruhen; nach 24 Stunden sind sie wieder
vollständig.

Entfällt: die Optionen `entity_room_actual` und `notify_service`.

## 0.18.0

Server-Update nötig, und zwar vorher. Danach die SmartHeat-Integration (ab Version 0.4.0) neu
einrichten: 0.18.0 startet mit der Konfiguration von 0.17.0 bewusst nicht und meldet im Log,
dass die Option `verteilsystem` fehlt. Dazu erst den bestehenden SmartHeat-Integrationseintrag
entfernen und danach die Integration neu hinzufügen — die Einrichtung erneut über den
bestehenden Eintrag laufen zu lassen, bricht wegen der Single-Instance-Sperre mit
`already_configured` ab.

Neu: Die Zeitfenster für Tag- und Nachtmittel und die Uhrzeit der täglichen Heizkurven-Anpassung
kommen vom SmartHeat-Server. Die Integration überträgt sie bei der Einrichtung.

Neu: Die lokalen Sicherheitsgrenzen (Heizkurve, Mindestvorlauf, Boost) richten sich nach dem
Verteilsystem statt nach dem Herstellerprofil. Für Heizkörper sind die Werte unverändert. Für
Fußbodenheizung gibt es noch keine Werte; das Add-on startet dann nicht.

Neu: Die Adresse des Abo-Service kommt aus der Konfiguration statt fest aus dem Add-on.

Entfällt: die Option `profile`.

## 0.17.0

Kein Server-Update nötig.

Neu: Kann die Anlage eine neue Heizkurve nicht übernehmen (z. B. weil die myVAILLANT-Cloud
gestört ist), meldet das Add-on das einmal als eigene Störung und versucht es automatisch
erneut. Bisher führte das nach etwa einer Minute fälschlich zur Meldung „Server antwortet
nicht“ und in den Notbetrieb.

Neu: Ist die Verbindung zum Server unterbrochen, sammelt das Add-on keine Messwerte für die
Heizkurven-Berechnung mehr zum späteren Nachsenden. Sobald die Verbindung wieder steht,
schickt es sofort frische Werte.

Verbessert: Doppelt zugestellte Serverantworten verlängern die Wartezeit bis zum nächsten
Versuch nicht mehr.

Verbessert: Ein Boost startet erst, wenn die Werte für die Rückkehr nach dem Boost sicher
gespeichert sind. Startet ein Boost, während schon einer läuft, übernimmt das Add-on die
Boost-Werte der Anlage nicht mehr fälschlich als Rückkehrwerte.

Intern neu gegliedert. Die Dateien in `/data` bleiben kompatibel; ein Wechsel zurück auf
0.16.0 ist ohne Weiteres möglich.

## 0.16.0

**Server-Update zuerst:** Der SmartHeat-Server sollte vor dem Add-on aktualisiert sein
(Plausibilitätsprüfung der Messwerte).

Neu: Antwortet der Server nicht, versucht das Add-on es sofort ein zweites Mal. Erst wenn
auch dieser Versuch unbeantwortet bleibt, beginnt der Notbetrieb. Danach versucht das
Add-on es nach 5, 15 und 60 Minuten und anschließend stündlich erneut. Sobald der Server
wieder antwortet, endet der Notbetrieb von selbst, auch über einen Neustart des Add-ons
hinweg, sofern beim Neustart noch ein Abgleich offen war (ein noch unter 0.15.0
begonnener Notbetrieb endet erst mit dem nächsten regulären Abgleich).

Neu: Liefert ein Sensor keinen gültigen Wert (z. B. leere Batterie), schickt das Add-on
keine Messwerte an den Server, meldet den betroffenen Sensor einmal und versucht es
regelmäßig erneut. Die Heizkurve bleibt so lange unverändert, ein Notbetrieb entsteht
dadurch nicht. Sind die Werte wieder da, kommt eine Entwarnung. Dasselbe gilt, wenn der
Server die Messwerte als unplausibel ablehnt.

Neu: Das Add-on beendet sich nicht mehr, wenn Home Assistant oder die Verbindung zum
Server beim Start noch nicht bereit sind. Es wartet und verbindet sich selbst.

Neu: Vor jedem Boost merkt sich das Add-on die aktuellen Heizkurvenwerte, damit es danach
sicher dorthin zurückkehrt.

Hinweis für bestehende Installationen: Die automatisch angelegten Statistik-Helfer (DAT,
DART, Raumtemperatur-Mittel) lassen sich in den Helfer-Einstellungen auf eine
Stichprobengröße von 10.000 stellen. Neue Installationen erhalten diesen Wert automatisch.

## 0.15.0

**Server-Update zuerst:** 0.15.0 spricht nur noch das neue Nachrichtenformat. Der
SmartHeat-Server muss vorher aktualisiert sein, sonst geht das Add-on nach dem ersten
Abgleich in den Notbetrieb.

Neu: Die Messwerte gehen als eine einzige Nachricht an den Server, die Antwort kommt als
eine Nachricht mit Status. Lehnt der Server die Werte ab (z.B. weil ein Sensor ausgefallen
ist), meldet das Add-on den Grund und behält die Heizkurve bei, geht aber nicht mehr
in den Notbetrieb.

Neu: Ist das SmartHeat-Abo inaktiv, zeigt Home Assistant eine Meldung "Abo inaktiv" an.
Die Heizung läuft dann noch 30 Tage im Notbetrieb weiter (Notfall-Boost bei kaltem Raum,
Komfort-Boost bei erhöhter Wunschtemperatur). Danach beendet sich das Add-on, die zuletzt
gelernten Heizkurvenwerte bleiben eingestellt. Nach Reaktivierung des Abos die
SmartHeat-Integration neu einrichten.

## 0.14.0

Neu: Sommersperre und genauerer Tagesabgleich. Das Add-on legt beim Start automatisch
einen weiteren Hilfssensor an ("SmartHeat <tenant> Aussentemp. 24h-Minimum") und merkt
sich die Solltemperatur-Aenderungen der letzten 24 h. Beides geht zusammen mit der Art
des Anlasses (taeglich / Solltemperatur geaendert) an den Server: War die Heizung die
ganzen letzten 24 h durch die Abschaltgrenze gesperrt, passt der Server die Heizkurve
nicht an. Beim taeglichen Abgleich vergleicht er die Raumtemperatur mit dem Mittel der
Solltemperatur statt mit dem aktuellen Wert. Keine Konfigurationsaenderung noetig; gegen
einen aelteren Server verhaelt sich das Add-on wie 0.13.1.

## 0.13.1

Fehlerbehebung: Der volle Snapshot an den Server enthielt seit 0.13.0 auch die
optionalen KPI-Sensoren. Der Sensor `operating_mode` (Text) scheiterte dabei an der
Zahlen-Umwandlung. Mit gesetztem `notify_service` kam dadurch bei jedem Snapshot eine
falsche Push-Meldung "Sensor liefert keinen gueltigen Wert", und der Server loggte je
KPI-Sensor eine Warnung. Der Snapshot enthaelt jetzt nur noch die 8 Rollen, die der
Server fuer die Heizkurvenberechnung braucht. `room_actual` und die KPI-Werte laufen
weiterhin ueber die Telemetrie. Keine Konfigurationsaenderung noetig.

## 0.13.0

Neu: das Add-on ist fuer optionale KPI-Entity-Mappings der erweiterten
Datenerhebungsphase vorbereitet. Die Zuordnung ueber die SmartHeat-Integration folgt
in einem spaeteren Integrations-Release. Vorbereitete optionale Felder:

- `entity_flow_temperature` (Vorlauftemperatur)
- `entity_return_temperature` (Ruecklauftemperatur)
- `entity_operating_mode` (Betriebsmodus)
- `entity_system_water_pressure` (Wasserdruck)
- `entity_efficiency_ratio` (Effizienzquote)
- `entity_energy_electrical_heating` (Elektrische Heizenergie)
- `entity_energy_electrical_dhw` (Elektrische Warmwasserenergie)
- `entity_energy_primary_heating` (Primaere Heizenergie)
- `entity_energy_primary_dhw` (Primaere Warmwasserenergie)
- `entity_energy_thermal_heating` (Thermische Heizenergie)
- `entity_energy_thermal_dhw` (Thermische Warmwasserenergie)

Alle diese Felder sind optional; fehlen sie, funktioniert das Add-on weiterhin wie zuvor.

## 0.12.0

**Breaking Change.**

**Der Fail-Safe-Alarm wird nicht mehr per fester Zeitschwelle ausgeloest, sondern
sofort, wenn ein vollstaendiger Snapshot-Publish innerhalb von 30 Sekunden keine
passende Antwort vom Server erhaelt** -- statt frueher erst nach
`failsafe_stale_after_hours` (Default 26h) ohne jede gueltige Server-Antwort. Das Feld
`failsafe_stale_after_hours` entfaellt ersatzlos.

Waehrend dieses "Notbetriebs" ueberwacht das Add-on den Raum weiterhin lokal: faellt
die Raumtemperatur mehr als 1 K unter die Solltemperatur, faehrt die Heizkurve
kurzzeitig auf die konfigurierten Maximalwerte (`curve_max`/`offset_max`), bis der
Raum wieder auf `boost_threshold_k` (Default 0.5 K) an die Solltemperatur
herangekommen ist -- danach zurueck zur zuletzt vom Server bestaetigten Heizkurve.
Dieser Zyklus kann sich beliebig oft wiederholen, solange Notbetrieb laeuft, und ist
unabhaengig vom bestehenden Comfort-Boost (Ausloeser: Sollwerterhoehung). Notbetrieb
endet automatisch, sobald der naechste regulaere Up-Snapshot-Versuch (taeglich oder
bei Solltemperatur-Aenderung, keine Aenderung der Kadenz) wieder eine passende Antwort
erhaelt.

**Achtung bei bestehenden Installationen:** ein zuvor gesetztes
`failsafe_stale_after_hours` in `options.json` wird ab dieser Version ignoriert (kein
Fehler). Kein manueller Schritt noetig -- der `binary_sensor.failsafe` bleibt
derselbe, zeigt jetzt nur den neuen Notbetrieb-Zustand statt des alten
Zeit-Watchdogs.

## 0.11.2

**Der Fail-Safe-Alarm (Stunden ohne Server-Antwort) konnte bisher durch einen
harmlosen MQTT-Reconnect (z.B. kurzer Haenger am Cloudflare-Tunnel) faelschlich
zurueckgesetzt werden und loeste dadurch praktisch nie aus.** Der Broker liefert
beim (Wieder-)Verbinden automatisch die zuletzt gesendete Down-Nachricht erneut
aus (MQTT "retained message") -- das wurde bisher wie eine frische Antwort des
Servers behandelt. Diese Wiederholungen werden jetzt erkannt und ignoriert;
echte Server-Antworten sind unveraendert sofort wirksam. Als Nebeneffekt bleibt
jetzt auch eine manuelle Korrektur des Sollwerts direkt am Pi zwischen zwei
Server-Antworten erhalten -- ein Reconnect ueberschreibt sie nicht mehr mit dem
veralteten wiederholten Wert. Keine Konfigurationsaenderung noetig.

## 0.11.1

**Eine schnelle Korrektur einer Solltemperatur-Eingabe loest jetzt keinen sichtbaren
Boost-Blip und keine unnoetige Server-Anfrage mehr fuer den zwischenzeitlich falschen
Wert aus.** Eine Solltemperatur-Aenderung gilt erst nach 10 Sekunden Stabilitaet als
final; erst dann wird geprueft, ob Boost noetig ist, und die Heizkurvenanpassung
angestossen. Betrifft sowohl Boost-Start als auch Boost-Ende. Keine
Konfigurationsaenderung noetig (das 10s-Fenster ist fest im Code).

## 0.11.0

**Boost und der volle Snapshot-Publish (Regelanpassung) reagieren jetzt sofort auf
Ereignisse statt auf den naechsten Poll zu warten.** Home Assistants interne
`subscribe_trigger`-Schnittstelle (dieselbe, die YAML-Automationen nutzen) liefert
Aenderungen an Raum-Ist-/Sollwert sowie den taeglichen Zeitpunkt jetzt direkt, ohne
Wartezeit. `local_check_interval_seconds` (Default jetzt 300s, Bereich 1-3600s) steuert
nur noch den Watchdog-/Fallback-Takt fuer den seltenen Fall, dass die
Websocket-Verbindung zu Home Assistant Core gerade unterbrochen ist -- dann poll't das
Add-on automatisch wieder wie bisher, bis die Verbindung zurueckkehrt. Wer diesen Wert
bereits manuell in `options.json` gesetzt hatte, muss nichts aendern: Werte bis 60s
(der alte Maximalwert) bleiben weiterhin gueltig.

## 0.10.2

Internes Bugfix-Release, keine Konfigurationsaenderung, zwei kleine Haertungen aus
dem finalen Whole-Branch-Review:

- `telemetry_interval_seconds` und `local_check_interval_seconds` lehnen jetzt auch
  `NaN`/`Infinity` als ungueltig ab, nicht mehr nur zu kleine bzw. zu grosse
  Zahlenwerte. Eine von Hand editierte `options.json` mit z.B.
  `"telemetry_interval_seconds": NaN` (gueltiges JSON) bestand die bisherige Pruefung
  unbemerkt, weil `NaN < 10`/`NaN > 60` in Python immer `False` ist, und hoehlte damit
  genau den Kadenz-Schutz aus, den diese Validierung eigentlich garantieren soll.
- Der allererste, synchrone Telemetrie-Check beim Add-on-Start (vor dem eigentlichen
  MQTT-Loop-Start) berechnet jetzt eine rein lesende Vorschau des Fail-Safe-Status
  (`_preview_failsafe_ctx`), damit dieser erste, veroeffentlichte Telemetrie-Datenpunkt
  bei einem Kaltstart einen frisch ausgewerteten `failsafe_active`-Wert traegt statt des
  beim Laden gesetzten, moeglicherweise veralteten Werts (der In-Memory-Kadenz-Marker
  uebersteht einen Neustart nicht, siehe 0.10.1-Note oben). Diese Vorschau liest nur:
  sie veroeffentlicht keinen MQTT-Status, schreibt `failsafe_state.json` nicht und
  loest keine Push-Benachrichtigung aus. Die eigentliche Fail-Safe-Zustandsaenderung
  (mit genau diesen drei Nebenwirkungen) entscheidet weiterhin ausschliesslich der
  erste Durchlauf der regulaeren Schleife, NACH dem MQTT-Loop-Start -- erst der liefert
  die retained Down-Nachrichten zu, die belegen, ob der Server tatsaechlich noch lebt.
  (Korrektur eines Entwurfsfehlers innerhalb dieses Releases, siehe
  Whole-Branch-Review-Fund I1: eine fruehere Fassung dieser Zeile rief hierfuer
  versehentlich die volle, zustandsaendernde Pruefung auf und konnte dadurch bei einem
  Neustart kurz vor Ablauf des 26h-Fensters eine falsche "Fail-Safe aktiviert"-Push-
  Benachrichtigung ausloesen, obwohl der Server durchgehend erreichbar war.)

## 0.10.1

Internes Bugfix-Release, keine Konfigurationsaenderung: die Telemetrie-Kadenz-Markierung
(`last_telemetry_publish_ts`) wird nicht mehr in `backup.json` persistiert (das haette
bei Standard-Intervall 300s ca. 288 zusaetzliche SD-Karten-Schreibvorgaenge/Tag verursacht
-- genau die Art SD-Verschleiss, die A.2 fuer die anderen `backup.json`-Felder bereits
eliminiert hat). Die Markierung lebt jetzt nur noch im Add-on-Prozessspeicher; ein
Add-on-Neustart verliert sie, was hoechstens eine harmlose zusaetzliche fruehe
Telemetrie-Veroeffentlichung verursacht.

## 0.10.0

Neu: `telemetry_interval_seconds` (optional, Standard `300`). Das Add-on
veroeffentlicht jetzt zusaetzlich in diesem Intervall eine rein beobachtende
KPI-Telemetrie-Nachricht ueber MQTT (`smartheat/<tenant_id>/telemetry`, nicht
retained) mit aktueller Raumtemperatur, Boost-Zustand und Fail-Safe-Zustand --
fuer serverseitiges KPI-/Qualitaets-Tracking. Dieser Pfad hat keinerlei
Einfluss auf die Heizungssteuerung (Heizkurve, Boost, Fail-Safe); ein Fehler
beim Veroeffentlichen wird geloggt und im naechsten Zyklus erneut versucht,
ohne den lokalen Check selbst zu stoeren.

**Achtung bei bestehenden Installationen:** kein manueller Schritt noetig --
die Integration setzt `telemetry_interval_seconds` derzeit nicht, das Add-on
verwendet einfach den Standardwert 300s. Fuer eine Aenderung dieses Werts
vorerst `options.json` auf dem Pi direkt anpassen (kein UI-Schritt dafuer in
dieser Version, wie schon bei den anderen optionalen Werten).

## 0.9.0

**Breaking Change.**

**Die eine `poll_interval_seconds`-Kadenz wird durch zwei getrennte Konzepte
ersetzt.** `poll_interval_seconds` (Standard 3600s) entfaellt ersatzlos.
`local_check_interval_seconds` (neu, Standard 30s, **hart begrenzt auf maximal
60s**) steuert ab jetzt nur noch den lokalen Boost-/Aenderungs-Check (liest
`target_rt`/Raumtemperatur direkt von Home Assistant, kein Server-/MQTT-Kontakt).
Der volle Snapshot-Publish an den Server (loest die serverseitige
Heizkurven-Neuberechnung aus) laeuft jetzt unabhaengig davon: einmal taeglich
zu einem festen, profilabhaengigen Zeitpunkt, oder sofort wenn sich die
Wunschtemperatur seit der letzten Veroeffentlichung geaendert hat.

`failsafe_stale_after_hours`s Standardwert steigt von `4.0` zurueck auf `26.0`
-- eine bewusste, im Rahmen dieser Aenderung erneut abgewogene Entscheidung
(nicht ein Widerruf der 4.0-Haertung vom letzten Update), siehe Design-Spec
`docs/superpowers/specs/2026-09-16-heizungsbruecke-trigger-kadenz-entkopplung-design.md`,
Abschnitt C. Der Wert entspricht dem historischen Vor-Haertungs-Default dieses
Add-ons, der bereits Puffer gegen die jetzt wieder eingefuehrte taegliche
Down-Nachrichten-Kadenz vorsah -- nicht der im Design-Spec urspruenglich
diskutierten Zahl: die Zahl hat sich erst waehrend der finalen Umsetzung von
`24.0` auf `26.0` verschoben, um genuegend Abstand zur ~24h-Kadenz zu behalten.

**Boost/Down-Message-Fix:** waehrend ein Boost aktiv ist, wird eine vom Server
eingehende Down-Nachricht nur noch in der internen Sicherung (`backup.json`)
gehalten, nicht mehr auf die Live-Entity geschrieben -- sie wurde sonst kurz
nach jedem Boost-Start durch die (vor-Boost) Server-Antwort ueberschrieben.
Beim Boost-Ende wird weiterhin automatisch der zuletzt gesicherte Wert
wiederhergestellt.

**Achtung bei bestehenden Installationen:** ein zuvor gesetztes
`poll_interval_seconds` wird ab dieser Version ignoriert (kein Fehler, das
Add-on verwendet einfach `local_check_interval_seconds`s Standardwert 30s).
Wer den lokalen Check-Takt aendern will, muss `local_check_interval_seconds`
direkt in `options.json` auf dem Pi setzen (kein UI-Schritt dafuer in dieser
Version, wie schon bei den anderen drei optionalen Werten) -- Werte ueber 60
werden beim Start mit einer klaren Fehlermeldung abgelehnt.

## 0.8.0

**Breaking Change.**

**Boost aktiviert nicht mehr bei kaltem Raum aus beliebiger Ursache.** Boost
reagiert ab dieser Version ausschliesslich, wenn die Wunschtemperatur erhoeht
wird (Komfort-Beschleunigung) -- nicht mehr, wenn der Raum aus anderer Ursache
(Aussentemperatur-Einbruch, offene Tuer, Server laengere Zeit nicht erreichbar)
kalt ist. Das bisherige Sicherheitsnetz-Verhalten entfaellt bewusst. Einziges
verbleibendes Signal fuer eine tote/veraltete Serververbindung ist ab jetzt der
Fail-Safe-Alarm (siehe unten).

`failsafe_stale_after_hours`s Standardwert sinkt von `26.0` auf `4.0` -- ein
mehrstuendiger Ausfall wird jetzt noch am selben Tag gemeldet statt erst nach
ueber einem Tag.

Neu: das Add-on prueft beim Start, ob der Tenant aktuell berechtigt ist (Abo
aktiv). Bei einem explizit als nicht aktiv gemeldeten Tenant startet das Add-on
nicht (klare Fehlermeldung im Log). Ein Netzwerk-/Serverfehler bei dieser
Pruefung selbst wird NICHT als "nicht berechtigt" gewertet (Fail-Open) --
ein kurzer accounts-api-Ausfall soll die Heizungssteuerung nicht stoppen.

MQTT-Nachrichten auf den `up`/`down`-Topics dieses Add-ons verwenden jetzt QoS 1
statt QoS 0 (zuverlaessigere Zustellung).

**Achtung bei bestehenden Installationen:** kein manueller Schritt noetig --
alle Aenderungen wirken automatisch nach dem Update. Wer sich auf Boost als
Reaktion auf einen kalten Raum verlassen hat, sollte das beruecksichtigen.

## 0.7.0

**Breaking Change.**

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

## 0.6.0

**Breaking Change.**

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
26.0h), wenn der Assistent sie nicht abfragt -- fuer eine Aenderung dieser drei
optionalen Werte vorerst `options.json` auf dem Pi direkt anpassen (kein
UI-Schritt dafuer in dieser Version).

## 0.5.0

**Breaking Change.**

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

## 0.4.0

Neu: `failsafe_stale_after_hours` (optional, Standard `26.0`). Das Add-on
veroeffentlicht jetzt einen `binary_sensor` ueber MQTT Discovery
(`heizungsbruecke_<tenant_id>_failsafe`), der aktiv wird, sobald seit mehr als
dieser Anzahl Stunden kein gueltiger Sollwert vom Server mehr angewendet wurde
-- kein `configuration.yaml`-Helper noetig, die Entity entsteht automatisch ueber
Home Assistants MQTT-Integration. Die Rueckschaltung auf Normal erfolgt erst nach
zwei aufeinanderfolgenden gueltigen Nachrichten (Anti-Flatter), unabhaengig von
dieser Schwelle.

## 0.3.0

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

## 0.2.0

**Breaking Change.**

Das Feld `profile` ist neu und **Pflicht** — bestehende Installationen muessen es
nach dem Update einmalig in der Add-on-Konfiguration setzen, sonst startet das
Add-on nicht (`FEHLER: Pflichtfeld 'profile' fehlt in der Add-on-Konfiguration`).
Ebenfalls neu: `entity_room_day_avg`, `entity_room_night_avg`,
`entity_heat_limit`, `entity_dat` und `entity_dart` sind jetzt Pflichtfelder
(vorher optional), und `notify_service` ist als optionales Feld hinzugekommen.
