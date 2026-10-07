# Changelog SmartHeat-Gateway

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
  prueft das Root-Dateisystem waehrend (Hook) und nach dem Bau (post-build)).
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
