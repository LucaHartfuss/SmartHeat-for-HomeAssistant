# Changelog SmartHeat-Gateway

## Unveröffentlicht

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
