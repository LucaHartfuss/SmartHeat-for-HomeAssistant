# Changelog SmartHeat-Gateway

## Unveröffentlicht

- Diagnose am Gerät (L4-18, Labor 2026-10-20): `iproute2` (`ss`, `ip`) ist im Image und wird von `install.sh` verlangt;
  `vcgencmd` kommt über `raspi-utils` im Image (Raspberry-Pi-Archiv, `rootfs_checks.sh` prüft alle drei; `install.sh` auf
  einem fremden Debian verlangt es nicht). Das Journal schreibt alle 30 s (`SyncIntervalSec=30s`, `Storage=persistent`
  in `journald.conf.d/smartheat.conf`), nach hartem Stromverlust fehlen höchstens die letzten 30 s. Die Container
  loggen über den Treiber `journald` (`tag` = Dienstname) statt `json-file`: `docker logs` scheitert nicht mehr an einer
  beschädigten Logdatei, und Größe und Rotation gelten für Host und Container gemeinsam. Mit dem nächsten Bundle werden
  die Container neu erzeugt (`compose up -d`). Lesen: `docker logs <Dienst>` oder `journalctl CONTAINER_NAME=<Dienst>`.
- Der Updater nennt sich beim Abruf von Manifest und Bundle-Dateien `SmartHeat-Gateway-Updater/<Version>`. Den
  Standard-User-Agent von Python sperrt die Browser-Integritätsprüfung von Cloudflare (HTTP 403, Fehler 1010), und das
  Update scheiterte als „nicht ladbar“ (Labor 2026-10-20, H2).
- Der SONOFF Dongle Plus MG24 (`10c4:ea60`, Produktname mit „MG24“) gilt als `ember`-Stick. Bisher ordnete die
  udev-Regel ihn als `zstack` ein, und Zigbee2MQTT startete endlos neu (Labor 2026-10-20, H2).
- Das Image fragt per DHCPv4 auf `eth0` mit der MAC als Client-ID (`eth_dhcp.sh`, Drop-in zu `01-eth0.network`;
  `rootfs_checks.sh` prüft es). Mit dem Standard von systemd-networkd (Client-ID aus der DUID) bekam das Gateway am
  Router des Labors nie eine IPv4-Adresse und damit keine Verbindung zum Server (Labor 2026-10-20, H2).
- Treibergrenzen (Nutzer-Entscheidung 22): `poll_seconds` darf höchstens 86400 s sein (ViCare mindestens 120 s wie bisher,
  Simulation mindestens 1 s und nur eine Zahl, kein Wahrheitswert und kein Text); eine Token-Antwort der Viessmann-IAM mit
  `expires_in` kleiner oder gleich 0 gilt als unbrauchbare Antwort. Der Server prüft dieselben Grenzen bei der Erkennung.
- Der Updater lehnt eine Weiterleitung beim Laden von Manifest und Dateien ab, wenn das Ziel nicht `https` ist (mit dem
  Labor-Schalter auch `http`; nie `ftp` oder `file`): das Ziel wird nicht angefragt, der Abruf gilt als nicht ladbar
  und wird im nächsten Durchlauf wiederholt. Bisher folgte `urllib` auch einer Weiterleitung von `https` auf `http`.
- Die Geräteliste der Einrichtung (`zigbee_devices`) lässt Messwerte weg, die der Link nicht senden kann: NaN, Unendlich,
  Ganzzahlen außerhalb des Zahlbereichs und Werte, die tiefer als 32 Ebenen verschachtelt sind; eine solche Batterie
  meldet sie als „unbekannt“. Bisher scheiterte die ganze Antwort, und die Geräteliste der Einrichtung blieb leer.
- Der Tagestick (`seconds_until`) feuert bei der Rückstellung der Uhr (Winterzeit, doppelte Stunde) nur einmal zur ersten
  Ortszeit der Tageszeit; bisher rechnete er in der zweiten Stunde mit einer negativen Wartezeit und feuerte bis zu eine
  Stunde lang dauernd.
- Jede Nachricht des Geräts entsteht über eine Stelle, die sie gegen den Vertrag prüft (`smartheat_device/nachrichten.py`),
  und das Gerät liest jede Vertragskonstante an der Stelle, die sie braucht (Stufe 3 S8a-1, A5-34). V1 gilt jetzt auf
  beiden Seiten strikt: Konfiguration, Bedienung und Befehle mit fehlendem oder fremdem Feld lehnt das Gerät ab. Der
  Befehl `driver_login` verlangt genau die Felder seiner Phase (`begin`, `finish`, `password`); Erkennung (Anlagen,
  Features) und Zigbee-Liste entstehen gegen den Vertrag. Neue Felder des Servers kommen damit erst nach einem
  Geräte-Update. **Das Gateway braucht den Server mit Stufe 3 S8a-1** (`S3S8a-Live`): Server und Gerät im selben
  Wartungsfenster, der Server zuerst; ein Server davor verlangt bei der Registrierung noch `enc_public_key`.
- Entfallen (Stufe 3 S8a-1, A5-43, A5-50): `enc_public_key` bei der Registrierung und die Datei `enc_key.pem` (eine
  vorhandene Datei bleibt liegen und wird nicht gelesen), `raum_soll_wirksam` im Snapshot und das Konfigurationsfeld
  `intervalle` (nur noch für die E2E über `SMARTHEAT_E2E_INTERVALLE`, ein JSON-Objekt; ein ungültiger Wert wird
  ignoriert).
- Das Status-Ereignis (Schema bleibt 3) trägt kein Feld `setup_id` mehr: es war seit 1.0.0 immer gleich `tenant_id`. Der
  Zustand (`backup.json`) speichert die Einrichtung als `tenant_id`; ein vorhandener Zustand mit `setup_id` wird beim
  Laden übernommen (keine neue Einrichtung, nichts geht verloren), der alte Schlüssel verschwindet beim nächsten
  Schreiben. Stufe 3 S7, A5-63.
- Nach einem unerwarteten Fehler der Laufzeit wartet das Gateway 60 s, trennt vorher sauber und startet dann neu (wie
  das Add-on); bisher endete der Prozess sofort und Compose startete ihn ohne Pause neu (Stufe 3 S6). Lebenszyklus und
  Gerätebindung teilen sich Gateway und Add-on jetzt im Gerätekern. Während der Pause meldet sich das Gateway ungesund
  (Lebenszeichen gelöscht, `/healthz` meldet die Laufzeit als `startet`): ein Update, dessen Laufzeit gleich nach dem
  Start scheitert, rollt der Updater weiter zurück.
- Die Wunschtemperatur `raum_soll` aus der Bedienung gilt als Kommazahl, auch wenn sie als ganze Zahl kommt (wie im
  Add-on, Stufe 3 S6).
- Beim Start gleicht das Gateway das Thermostat nur an, wenn eine gültige Bedienung (`raum_soll`) vorliegt; ohne sie
  bleibt das Thermostat unberührt (bisher auf den Standard von 20 °C, Stufe 3 S6).
- Die Bootstrap-Antwort wird strenger geprüft: Port nur 8883 oder 443, ALPN nur fehlend oder `x-amzn-mqtt-ca`; eine
  andere Antwort gilt als ungültig, der Bootstrap wird später wiederholt (Stufe 3 S6).
- Intern: Image-Scan (`trivy`) über Gateway-Image und Fremd-Images der Compose-Datei in `scripts/check.sh --full`; ein
  kritischer Befund mit Korrektur ist rot (Stufe 3 S6).
- Der Befehl `driver_probe` entfällt: die Erkennung der Anlagen ist die Inventur mit `stunden: 0`; das Gateway liefert die
  gekürzten Rohdaten der Hersteller-Schnittstelle, die Bewertung in Kandidaten macht der Server (Stufe 3 S5b, G-6).
- Die Diagnoseseite zeigt QR-Code und Übernahme-Code nur, solange das Gateway nicht übernommen ist (Spec 5c F1); nach
  dem Entfernen erscheint ein neuer Code.
- Intern (Phase 1 S1b): Das Image enthält die ungenutzten Module `smartheat_runtime/mqtt_link.py` und
  `smartheat_transport/mqtt_client.py` nicht mehr (Passwort-Transport entfernt); keine Verhaltensänderung.

## 0.6.0

Spec 5b (Geräteprotokoll über MQTT), Plan D2. Nur für Labor-Gateways; im Feld gibt es keine Gateways, einen Übergang
von 0.5.0 gibt es nicht (Spec 5.3). Kein Release-Tag in 5b ohne echten Signierschlüssel (Runbook 13.1).

- Ein Gateway-Container `runtime`: Gerätekern `smartheat_device` (Bootstrap mit Zertifikat, MQTT 5 über TLS zu
  `smartheat/<geräte_id>/…`, Soll-Dokumente, Befehle, Bedienwunsch, Inventur) und Laufzeit in einem Prozess; Tick und
  Telemetrie über denselben Link. Agent-Container, HTTPS-Polling, Tunnel (`cloudflared`) und Netz-Wache entfallen.
  Compose gibt `runtime` 20 s `stop_grace_period`; der Gerätethread wird beim Beenden abgewartet und danach noch
  einmal gestoppt (ein im letzten Takt geöffneter Link wird ebenfalls getrennt). Die Soll-Software wird beim Start
  erneut an den Updater gegeben.
- Befehle mit Cloud-Aufrufen (`driver_login`, `driver_probe`, die Proben von `inventory`) laufen in einem eigenen
  Hintergrund-Thread; der Gerätethread wartet nie auf den Hersteller, `down/setpoints`, Dokumente und `hello` laufen
  weiter. Antwortet der Treiber bei Anmeldung oder Probe nicht bis zur Frist des Befehls (120 s), endet er mit
  `anlage_nicht_erreichbar`.
- Konfiguration und Bedienung kommen als Dokumente; eine laufzeitrelevante neue Konfiguration startet den Prozess nach
  einem sauberen MQTT-DISCONNECT neu. Leere Konfiguration nach einer Einrichtung: der nächste Start löscht den
  anlagengebundenen Zustand und die Treiber-Anmeldungen (A4-07).
- `sign_off` setzt die Anlage über einen Neustart zurück und läuft auch dann, wenn der Server den Befehl nach dem
  Neustart erneut zustellt (Befehlsart „wiederholbar“, kleine Ergänzung im Gerätekern).
- Thermostat: eine Drehung ist ein Bedienwunsch (am Ende der Entprellung, begrenzt auf 15–25 °C, zurückgeschrieben);
  `system_mode: off` oder ein Soll ≤ 7 °C ist „Soll-Quelle aus“ (Meldung, kein Wunsch); ein noch offener Wunsch wird
  dabei verworfen und das vorherige Soll bleibt gültig. Wieder an (`system_mode` nicht mehr `off`, kein Frostschutz):
  das geltende Soll geht ans Thermostat, die alte Soll-Meldung danach ist kein Wunsch (eine Bedienung während „aus“
  bleibt gültig); Frostschutz durch Drehen verlassen bleibt ein Wunsch.
- Updater: Soll-Version aus dem Feld `software` der Konfiguration (`data/software_soll.json`), Ergebnisse über die
  Laufzeit als `up/result` (`dokument: "software"`); Erreichbarkeit = Broker per TCP (die Uhr zählt dafür nicht mehr);
  `REQUIRED_HEALTHY = ("runtime",)`; `smartheat_host/device_api.py` → `kanal.py`. Die Host-Dienste (root) lesen vom
  Container beschreibbare Dateien nur begrenzt und ohne Symlinks zu folgen (`smartheat_host/lesen.py`);
  `files._write_bytes` schreibt mit `O_NOFOLLOW`, `dir_fd` und `O_EXCL`. **Host-Dienste ändern sich: `install.sh` auf
  Labor-Gateways erneut ausführen.**
- LED und Diagnoseseite: neue Zustände `gesperrt` und `update_noetig`; die Seite zeigt Hinweise des Dienstes, eigene
  Meldungen und eine abgelehnte Konfiguration; QR-Code und Übernahme-Code, solange das Gateway nicht eingerichtet ist.
- Lokaler Bus nur noch für Zigbee2MQTT (ACL `runtime`/`zigbee2mqtt`); `uhr_synchron` aus `host/status.json`.
- Ein dauerhaft scheiterndes Schreiben der LED-Datei loggt nur beim Wechsel der Fehlerart und bei der Erholung; im
  Ruhezustand geht eine Bedienung nicht mehr an den Worker (sie gilt beim nächsten Start mit Regelung).

## 0.5.0

- Neu: Liefer-Signale (Betriebsstunden, Starts, Status, Betriebsart) aus der Viessmann-Cloud und im Simulations-Treiber
  (Audit 4 P-B). Die Feature-Namen sind aus PyViCare abgeleitet und an einer echten Anlage noch nicht geprüft (G4-Pilot).
- Laufzeit: Übernahme alter `backup.json`-Felder aus Add-on 0.29.0 entfernt (betrifft das Gateway nicht, Audit 4 P-E).

## 0.4.0

Plan SHG G4: Treiber `vicare_cloud` (Viessmann-ViCare-Cloud) und Audit 4 Paket P-C2 (Gateway-Punkte). Die Version 0.4.0
wird mit dem Add-on-Release `heizungsbruecke` 0.35.0 vergeben, weil sich Code unter `gateway/` geändert hat (Release-Gate);
das Gateway-Image selbst ist noch nicht veröffentlicht, sein Release (Tag `gateway-v0.4.0`) bleibt getrennt.

- Neuer Treiber `vicare_cloud` (`drivers/vicare_cloud/`): `PlantBinding` mit derselben `VIESSMANN_VICARE_BINDING` wie der
  HA-Pfad, eigener dünner `requests`-Client der ViCare-REST-API (PyViCare ist keine Laufzeit-Abhängigkeit), Schreibgruppe
  Steigung/Niveau als zwei `setCurve`-Aufrufe (seit Audit 4 P-C2 ein Aufruf, siehe unten) mit Überlagerung, Programmwechsel mit Einschwingfenster, Wertebereich der
  Anlage aus den Kommando-Constraints (`limits()`).
- Anmeldung per OAuth2 mit PKCE (Variante A, **vorläufig bis zum Eingangs-Gate** mit dem echten Viessmann-Client): Tokens
  nur in `/data/secrets/drivers/vicare.json` (0600), Erneuerung unter `flock`, abgemeldet wird nur bei `invalid_grant`.
- Kontingent-Wächter angeschlossen (1450/Tag, harte Grenze 1200, 429 sperrt bis `Retry-After`); Thread-Vertrag der Treiber
  (ein `RLock`, auch im Simulations-Treiber); Treiber-Cache nach drei Abfrageintervallen veraltet.
- Probe liefert Kandidaten mit Hebelsatz `viessmann_vicare` oder einem von fünf Ablehnungsgründen
  (`heizkurve_nicht_schreibbar`, `kein_normalprogramm`, `schrittweite_abweichend`, `erzeuger_unbekannt`,
  `aussentemperatur_fehlt`).
- Inventur im Agenten: Probenreihe `/data/agent/inventory.json` (alle 900 s eine Probe, setzt nach Neustart und erneuter
  Zustellung fort, wird beim Einrichten und Abmelden gelöscht); `driver.inventory(stunden, proben)` ohne Seriennummern.
- Tests: Matrix aus den PyViCare-Aufzeichnungen (gepinnter Commit, Lizenzhinweis) plus eigenen, Fake-ViCare-Server
  (`tests/fake_vicare/`, auch als E2E-Container), Gleichheitstest gegen den HA-Pfad, Szenario „Anmeldung abgelaufen“.
- Bekannt: `urllib3` loggt auf Stufe DEBUG URLs mit Seriennummer (Logger noch nicht auf WARNING begrenzt, Roadmap).
- Audit 4 Paket P-C2 (Gateway-Punkte):
  - Zustand an Einrichtung und Anlage gebunden: `backup.json` trägt `setup_id` und `plant_id`; ein Bestand ohne Bindung
    gilt als gebunden und wird nur ergänzt. Eine Neueinrichtung oder „Neu konfigurieren“ derselben Anlage behält die
    gemerkten Ursprungswerte; eine andere Anlage wird nach Abmeldung erst nach bestätigtem Rückweg zugelassen und
    sonst abgelehnt (`konfiguration_ungueltig` mit eigenem Text). Ein bereits eingerichtetes Gateway lehnt die
    Konfiguration einer anderen Anlage ebenfalls ab; „Neu konfigurieren“ geht nur für dieselbe Anlage.
  - ViCare: eine `setCurve`-Anfrage je Schreibgruppe (Steigung und Niveau zusammen) statt zwei; zählt als ein
    Schreibzugriff im Kontingent.
  - Manifest wird nach der ersten Abfrage gebaut (optionale Signale wie `flow_temperature` sind enthalten); der Treiber
    prüft seine Parameter, `poll_seconds` (gilt für den Treiber `vicare_cloud`) muss mindestens 120 s sein.
  - Anmeldung (ViCare/OAuth2) überlebt einen Widerruf des Refresh-Tokens: eine im Portal vorbereitete Anmeldung lässt sich
    abschließen.
  - Statusmeldung wird nur bei einer echten Änderung (oder spätestens alle 300 s) gesendet, nicht mehr wegen des
    Zeitstempels des Raumwerts.
  - Abmelden löscht auch das Installations-Token.
  - Diagnoseseite nur aus dem lokalen Netz (private und Link-Local-Adressen, sonst 403). Bekannt: Eine Anfrage, die über
    docker-proxy ankommt (IPv6, Hairpin), erscheint mit einer privaten Adresse; der Router darf Port 80 deshalb nicht ins
    Internet weiterleiten.

## 0.3.0

Plan G2b-2 Teil A (Basis-Image und Pilot-Zugang, ohne Hardware geprueft). Erstes echtes Release folgt im
Hardware-Gate (Teil B) mit dem echten minisign-Schluessel.

- Basis-Image mit `rpi-image-gen` v2.8.0 (`gateway/image/`): Raspberry Pi 4, Debian 13, Host-Installer im chroot
  (`install.sh --image`), Container-Images als Archive fuer den Erststart ohne Pull (`smartheat-firstboot`),
  Pruefung des Root-Dateisystems (keine geteilte Identitaet, kein Passwort, kein Tunnel-Token, SSH nur im
  Pilot-Image). Release-Workflow haengt das Serien-Image an das GitHub-Release. Der echte Bau ist ohne arm64-Host
  noch nicht gelaufen (Hardware-Gate bzw. CI-Spike); der `-dryrun` baut das Image nicht.
- Eigener Hostname statt IP oder AWS-Adresse (Nutzer-Vorgabe): Geraete-API- und Portal-Adresse im Image sind immer
  `https://<eigener DNS-Name>`; `gateway/image/own_url.sh` lehnt IP-Adressen, Einzel-Label-Hosts, Port, Pfad und
  AWS-Namen ab (`prepare.sh`, `make_image.sh` und die Workflows pruefen die Adressen vor dem Bau; `rootfs_checks.sh`
  prueft das Root-Dateisystem waehrend (Hook) und nach dem Bau (post-build)). Zusaetzlich Allowlist: nur Namen in der
  eigenen Zone `hartfussha.org` (die Zone selbst oder ein Name darunter, `SHG_OWN_ZONES` in `own_url.sh`); die
  Denylist bleibt als Tiefenverteidigung.
- Zeitzone Europe/Berlin und Standard-Locale de_DE.UTF-8 (Tastatur de) im Image (Abschnitt `locale` der
  Image-Konfiguration; Pakete `locales` und `tzdata` im Layer, `locale_default.sh` setzt LANG, weil `locale-base` von
  rpi-image-gen v2.8.0 `LANG=C.UTF-8` schreibt); `rootfs_checks.sh` prueft `/etc/localtime`, `/etc/locale.conf` und `/etc/default/keyboard`.
- WLAN aus in jedem Image (das Gateway laeuft nur am Ethernet): `wlan_off.sh` maskiert `iwd.service`, entfernt
  `02-wlan0.network` und setzt `dtoverlay=disable-wifi` in der `config.txt`; Bluetooth bleibt unberuehrt (kein
  Bluetooth-Dienst im Image). `rootfs_checks.sh` prueft alle drei Stellen.
- Erststart ohne Pull: Container-Images werden mit `skopeo copy --all --preserve-digests` als OCI-Archive exportiert;
  das Geraet laedt sie in den containerd-Speicher von Docker (`/etc/docker/daemon.json`, von `install.sh` vor der
  Docker-Installation geschrieben; nur frische Geraete). Die Root-Partition ist fest 8G gross (kein Wachstum beim ersten
  Start).
- Pilot-SSH-Tunnel (`gateway/host/pilot_ssh_tunnel.sh`): eigener Cloudflare-Tunnel nur fuer SSH, Host-Dienst
  ausserhalb von Docker, cloudflared 2025.8.1 per SHA-256 gepinnt; `install.sh` bricht ohne `--pilot-ssh` ab,
  solange der Tunnel eingerichtet ist.
- Updater: nur streng neuere Versionen (Downgrade-Schutz, Grund `version_zu_alt`).
- Updater: ein Rueckweg, weil der Server waehrend der ganzen Gesundheitspruefung nicht erreichbar war, meldet den
  Grund `server_unerreichbar` statt `ungesund` (die Version wird erneut versucht); `ungesund` heisst jetzt immer
  abgelehnt.
- Init-Schritt heilt Bus-Drift: Passwortdatei passend zu den Zugangsdateien, Zugangsdaten in der
  Zigbee2MQTT-Konfiguration angeglichen (Netzschluessel bleibt); eine unlesbare oder beschaedigte ACL-Datei wird neu
  geschrieben statt den Init-Schritt abzubrechen.
- Thermostat: ein offener Schreibbefehl verwirft verspaetete Meldungen des alten Sollwerts (bis 30 min); jede andere
  Meldung gilt als Eingabe am Thermostat.
- Automatische Updates auch fuer alle Pakete des Raspberry-Pi-Archivs (vor allem Kernel und Firmware), ohne automatischen
  Neustart.
- Drift-Tests: ACL gegen alle Bus-Aufrufe, Updater-Fixture gegen die Geraete-Compose.

## 0.2.0

Plan G2b-1 (Geraetesoftware ohne Hardware). Noch nicht veroeffentlicht: der erste Release braucht den echten
minisign-Schluessel (Runbook, Abschnitt Gateway-Release); bis dahin ist `release.pub` ein Platzhalter.

- Bus-Schutz: neuer einmaliger Compose-Dienst `init` (vor allen anderen) erzeugt je Dienst (`agent`, `runtime`,
  `zigbee2mqtt`) ein zufaelliges Passwort (`bus/credentials/<dienst>/bus.json`, 0600), die Mosquitto-Passwortdatei und
  eine ACL je Dienst (`bus/mosquitto/`), die Zigbee2MQTT-Grundkonfiguration (Schreiber ist jetzt `init`, nicht mehr der
  Agent) sowie `data/device` und `data/agent`. Mosquitto laeuft mit `allow_anonymous false` als uid 1000; eine
  beschaedigte Zugangsdatei wird neu erzeugt und die Passwortdatei neu aufgebaut. Agent und Laufzeit melden sich ueber
  `SHG_BUS_CREDENTIALS` an.
- Geraeteschluessel und Agent-Zustand (`/data/device`, `/data/agent`) sind in `tunnel` und `runtime` durch einen
  schreibgeschuetzten, leeren tmpfs verdeckt.
- Netz-Wache: die Laufzeit beendet sich (Compose startet sie neu), wenn der lokale Bus laenger als
  `SHG_BUS_LOST_EXIT_SECONDS` (Standard 300) getrennt ist, etwa nach einem Neustart des Tunnel-Containers.
- Host-Dienste (`gateway/host`, Paket `smartheat_host`, System-Python): Updater (signierte Soll-Version, Manifest mit
  SHA-256 und minisign-Signatur, Umschalten per Compose, Gesundheitspruefung, Rueckweg, `update_result`), LED-Muster je
  Agent-Zustand und Hoststatus (`host/status.json`: Netz, DNS, Zeit). Der Updater wertet Umgebungsfehler nicht als
  Fehler des Bundles: die Gesundheitsfrist ruht, solange der Server nicht erreichbar ist (hoechstens 60 min, danach
  Rueckweg ohne Ablehnung), ein fehlender Zigbee-Stick verschiebt das Umschalten, nicht gesendete `update_result`
  werden im naechsten Durchlauf nachgeholt.
- Host-Installer `gateway/host/install.sh` (idempotent, Debian 13 trixie): Pakete, Ordner, Host-Paket, systemd-Units,
  udev-Regel fuer den Zigbee-Stick, nftables-Firewall, journald-Grenze, Sicherheitsupdates, optional Pilot-SSH.
  Geprueft im Debian-Container (amd64, CI zusaetzlich arm64).
- Signierte Bundles: `gateway/release/build_bundle.py` (Compose-Datei mit Image-Digests, Manifest), eigener
  minisign-Pruefer auf dem Geraet, `release.pub` als Platzhalter bis zum echten Schluessel.
- Release-Workflow `release-gateway.yml` (Tag `gateway-vX.Y.Z`, `-dryrun` mit Wegwerf-Schluessel und lokaler Registry),
  `tools/release_gate.py --kind gateway` im Dev-Root, CI-Job `install-arm64`. Der Signier-Job baut das Bundle aus dem
  Tag neu und signiert nur bei byte-gleichem Ergebnis (`gateway/release/verify_bundle.py`); Python-Pakete des Workflows
  nur aus der Hash-Lock-Datei `gateway/release/requirements.txt`.
- Fixes aus G2a: retained Zigbee-Werte gelten bis zur ersten Live-Meldung als veraltet und loesen keine Geraete-Hooks
  aus; Tagestick ueber `zoneinfo` (richtig beim Wechsel Sommer-/Winterzeit); Kontingent-Zaehler atomar mit `fsync`;
  eigener Hinweis bei fehlender Accounts-URL (Texte des Add-ons unveraendert); kein Aufraeumen, waehrend ein `sign_off`
  wartet; geschlossene Testluecken.

## 0.1.0

- Erste Fassung (Plan G2a): Agent, Laufzeit mit SHG-Host, lokaler Bus, Zigbee2MQTT-Anbindung, Simulations-Treiber,
  Compose. Noch nicht veroeffentlicht (Release ab Plan G2b, Tag `gateway-v*`).
