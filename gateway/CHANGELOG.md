# Changelog SmartHeat-Gateway

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
