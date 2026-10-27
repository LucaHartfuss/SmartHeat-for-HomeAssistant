# Changelog SmartHeat-Gateway

## 0.9.0

- Uebernahme-Code nur nach einem Knopfdruck am Geraet (SG-02, GPC21): Der Container wertet `host/knopf.json` des
  Knopf-Dienstes; ein Druck ist eine Aenderung des Paars (`seq`, `ts`), die erste Lesung nach dem Start ist nur Grundlinie
  (ein Neustart oeffnet kein Fenster). Jeder Druck erzeugt einen neuen Code (`Device.neuer_uebernahme_code`, derselbe Weg
  wie der Befehl `new_claim_code`), hoechstens einer je 60 s, ein Druck davor verlaengert nur das Fenster. Die Seite zeigt
  Geraete-ID und Code 15 min lang (nur im Speicher), sonst nach einem Druck "Keine Verbindung zum Server – kein Code"
  bzw. "uebernommen". `agent_state.json` meldet das offene Fenster als `code_fenster` (LED `code_sichtbar`).
- Systemteil im Updater (AP-05a, GW 6.3): Traegt das signierte Manifest das Feld `system` (`system.raucb`, SHA-256, Groesse
  bis 2 GiB, `compatible` `smartheat-gateway-pi5`), wechselt der Updater zuerst das System (A/B, RAUC, Neustart mit tryboot)
  und danach die Container. Ablauf mit gespeicherten Phasen (`system.phase` in `updater/state.json`, **vor** jeder Wirkung):
  installiere, installiert, probestart, bestaetigt, oder zurueck (mit Grund und `ablehnen`). Im Probestart gilt die lokale
  Gesundheit fest 600 s ab dem Start des Updaters (Host-Dienste, Container, `runtime` healthy, `/healthz` ohne `startet`,
  ohne `server_ok`), danach `mark-good` und eine Netzprobe von 600 s; `current`/`previous` wechseln erst danach. Rueckfaelle:
  Fehlstart = `start_fehlgeschlagen` (abgelehnt), ungesund = `ungesund` (abgelehnt), Netzprobe und Strom weg =
  `server_unerreichbar` ohne Ablehnung mit Abstand 1 h, 2 h (hoechstens 24 h, Wanduhr, erst ab der ersten synchronen Uhr),
  der dritte in Folge lehnt ab. `rauc install` scheitert einmal vorlaeufig, zweimal in Folge `manifest_ungueltig`. Ohne
  erreichbaren Broker beginnt kein Systemwechsel; ein falsches `compatible`, eine falsche Pruefsumme oder Groesse sind
  `manifest_ungueltig`. Das System-Bundle liegt unter `updater/system/<Version>.raucb` (root, 0600) und wird nach
  Bestaetigung, Ablehnung oder Rueckfall geloescht. Der Erststart startet nichts, solange ein Systemwechsel offen ist.
- Downgrade-Schutz mit Untergrenze `max(current, Version des Host-Pakets)` (A7-22): auch ohne `state.json` (frisches oder
  zurueckgesetztes Gateway) wird kein Bundle angenommen, das nicht streng neuer als der laufende Updater ist.
  `State.save` erhaelt Felder, die der Updater nicht kennt (ein aelterer Updater nach einem Rueckfall verwirft nichts).
- `min_updater_version` der Bundles: 0.8.0 ohne Systemteil (der Updater 0.8.0 nimmt Container-Bundles weiter an), 0.9.0 mit
  `system`. `build_bundle.py --system DATEI` bindet das System-Bundle im Manifest, `verify_bundle.py` prueft Groesse und
  SHA-256 der Systemdatei.
- Image fuer den Pi 5 (AP-05a, GW 6.1): `rpi-image-gen` v2.9.0 mit Layout `image-rota` statt `image-rpios`, NVMe statt USB
  (`usbboot.sh`, `initramfs_check.sh`, feste MBR-Signatur und der Pi-4-Pfad entfallen). GPT mit sieben Partitionen:
  `bootconfig`, `boot_a`/`boot_b` (je 256M), `system_a`/`system_b` (je 6G, ext4 mit 16-K-Bloecken, nur lesend), `werk` (16M,
  ext4, `/werk` mit `ro,nodev,nosuid,noexec`; `hooks/preimage50-werk` fuegt sie in `genimage.cfg` ein) und `persistent`
  (= `daten`, 4G). Slot-uebergreifend sind `/var/lib/{smartheat,docker,containerd,rauc,systemd/timesync}`. Kernel mit
  `panic=10`, Hardware-Watchdog (`RuntimeWatchdogSec=10s`, `RebootWatchdogSec=5min`), WLAN und Bluetooth per Overlay
  (`disable-wifi-pi5`, `disable-bt-pi5`) aus. Pakete: `rauc`, `rauc-service` (`/etc/rauc/system.conf` aus
  `host/rauc/system.conf.in`; `rauc-mark-good.service` maskiert, bestaetigt wird nur vom Updater) und Docker 29 aus dem Docker-Archiv
  (`docker-apt/`: Schluessel mit Fingerabdruck, vier Pakete exakt gepinnt); kein `docker.io`, kein `unattended-upgrades`
  (`host/apt/` entfaellt, der Paketstand aendert sich nur mit einem neuen System-Bundle). Die Pruefungen des Bauergebnisses
  laufen als Hooks (`hooks/postbuild50-rootfs-checks`, `hooks/finalize10-bootfs-checks`; v2.9.0 ignoriert den alten
  `post-build.sh` ohne Meldung). `make_image.sh` liefert zusaetzlich `system-<Version>.ext4`, `boot-<Version>.vfat` und die
  SBOM (`smartheat-gateway-<Version>.sbom.zst`, syft, CycloneDX-JSON laut `image/sbom/syft.yaml`, S-10), je mit `.sha256`; `--pilot-ssh` bricht bis AP-05b ab.
- Ablage im System, erster Start auf `daten`, Partition `werk` (AP-05a, GW 6.1, FR-26, GPC31): Das Image legt System-Teile nur
  unter `/opt/smartheat/` ab (Host-Paket, `release-roots.pem`, `rauc-backend`, `probestart-frist`, `gateway.env` als einzige Quelle
  fuer Units und Compose, `werkseinstellung/` mit erstem Bundle, `state.json`-Vorlage und den Container-Archiven als
  `images/<SHA-256>.tar`); `host/gateway.env` unter `SHG_ROOT` entfaellt. Unter `SHG_ROOT` (= `daten`) legt der Bau nichts ab:
  `smartheat-ablage.service` gleicht bei jedem Start Ordner, Besitzer und Modi an (`bus/mosquitto-daten` neu, Symlinks werden
  nicht durchschritten) und legt beim ersten Start das erste Bundle und `updater/state.json` aus der Werkseinstellung an.
  `smartheat-firstboot.service` laedt jedes Archiv einmal (Merker `host/geladen/<SHA-256>`, Archive bleiben liegen) und ruft bei
  jedem Start `compose up` fuer das laufende Bundle; der Updater wartet nicht auf das Laden (nur auf Docker, Netz und Ablage:
  die Probestart-Frist). `smartheat-werk.service` prueft vor dem Netz `/werk/werk.json` (streng: ein Feld mehr, falscher Typ,
  Symlink oder ueber 4 KiB sind ungueltig), kopiert die Werte nach `SHG_ROOT/host/werk.json` (0644) und setzt den fluechtigen
  Hostnamen; ungueltig oder fehlend loescht die Kopie. `smartheat-probestart-frist.timer` ist im Image aktiviert. `prepare.sh`
  nimmt das eigene Image aus `gateway-image.oci.tar` des Bundles (Groesse, SHA-256 und Index-Digest gegen `manifest.json`) und holt
  nur Mosquitto und Zigbee2MQTT per `skopeo`; die Pilotzweige der Bau-Skripte entfallen.
  Updater und Erststart teilen eine Compose-Sperre (`updater/compose.lock`, flock): kein `docker compose up` zweier Bundles
  zugleich. Die Ablage haelt Updater, Hoststatus und Erststart nicht auf (`Wants=`); ein untergeschobener Eintrag im
  Container-Bereich wird gemeldet und nicht angefasst. Das Image hat keinen statischen Hostnamen (Rueckfall
  `DEFAULT_HOSTNAME=smartheat-gateway` in os-release), damit der Name aus `werk.json` gilt.
- Waechter fuer die Laufzeit (AP-05a, GA-09, FR-35): `smartheat-waechter.service` (Takt 30 s) startet `runtime` mit `docker
  compose -p smartheat restart runtime` (Frist 120 s) neu, wenn der Compose-Healthcheck dreimal in Folge `unhealthy` meldet;
  hoechstens drei Neustarts je Stunde, der vierte wird nicht ausgefuehrt (ein ERROR im Journal). Pausiert bei einem offenen
  Container-Auftrag, in den Phasen installiere, installiert und probestart, bei `host/werksreset.json` und solange der Updater
  oder der Erststart die Compose-Sperre haelt (der Waechter wartet dort nie).
- Firewall und Docker-Pool (AP-05a, GW-Weiterleitung): `docker-daemon.json` pinnt `default-address-pools` auf `172.30.0.0/16`
  (Groesse 24); die Kette `prerouting` (raw) in `table inet smartheat` verwirft Pakete an 127.0.0.0/8 von ausserhalb von `lo`
  und an den Docker-Pool von allen Schnittstellen ausser `docker0` und `br-*` (kein Direktrouting aus dem LAN an `runtime:8081`).
- Mosquitto behaelt seinen Zustand (L4-28): `persistence true` mit `bus/mosquitto-daten` als `/mosquitto/data`.
- `cloudflared.pin` auf 2026.10.5 (SHA-256 beider `.deb` aus dem GitHub-Release berechnet und mit dem Release verglichen).

## 0.8.0

- Signierte Auslieferung (AP-04): Der Updater prueft das Manifest mit einer X.509-Kette (`release-roots.pem`,
  Release-Zertifikat mit `codeSigning`, ECDSA P-256) statt mit minisign, versucht ein Update nur mit synchroner Uhr
  (`wartet_auf_zeit`, sonst ohne Abruf und ohne Ablehnung) und lehnt Zertifikate ab, die aelter sind als das juengste
  angenommene (Widerruf eines gestohlenen Schluessels). `rejected` fuehrt `<Version>@<SHA-256 ueber Fingerabdruck des
  Release-Zertifikats + manifest_sha256>`: eine mit einem anderen Zertifikat neu signierte Fassung oder ein anderes
  Manifest derselben Version wird wieder versucht, anderer Leerraum im Signaturtext oder die zweite ECDSA-Form
  (s -> n - s) ergeben keine neue Kennung. `install.sh` setzt die Zertifikat-Untergrenze beim Anlegen von `state.json`
  aus dem `notBefore` des Zertifikats im mitgelieferten Bundle (eine Signatur ohne lesbares Zertifikat bricht den
  Installer ab). Die Signaturdatei heisst `manifest.json.sig`.
- Das eigene Image kommt als OCI-Archiv im Bundle (Manifest-Feld `archiv`: `gateway-image.oci.tar`, SHA-256, Groesse bis
  512 MiB, Name `smartheat-gateway`, Index-Digest) statt aus GHCR. Der Updater holt es nach der Kettenpruefung (Frist
  60 s plus Groesse bei mindestens 2 Mbit/s, Fortsetzen nach einem Abbruch per HTTP-Range, Ablage unter
  `archive/<Version>.oci.tar`), prueft Groesse und SHA-256, laedt es mit `docker load` (Frist 600 s) und verlangt, dass der
  Docker `smartheat-gateway@<Index-Digest>` kennt (sonst `manifest_ungueltig`); danach wird das Archiv geloescht. Ist
  das Image schon da, wird nichts geholt. Die Compose-Datei nennt es mit `pull_policy: never`; `check_compose` erlaubt
  `never` nur fuer genau dieses Image. Der Rueckweg braucht kein Netz: `prune` behaelt das Image der vorigen Version.
- Das Release wird am Betreiberrechner gebaut, signiert und per `scp` in die eigene Ablage geladen
  (`gateway/release/release_lokal.sh`, Schluessel auf einem Hardware-Token). Das Skript prueft den Tag gegen
  `.github/allowed_signers` aus `origin/main` (nicht aus dem Checkout), erzeugt das CycloneDX-SBOM des Archivs neben dem
  Bundle (gepinnter syft-Container) und lehnt mit `--probe` einen `pkcs11:`-Schluessel und den Standard-Schluesselbund ab.
  Die CI baut und signiert nichts mehr: der Workflow `release-gateway.yml` prueft nur (volle CI, Probelauf mit
  Wegwerf-Kette, Gate in einem eigenen Job ohne Schreibrecht, Tag-Signatur) und rueckt `main` per Fast-Forward vor, ohne im
  Job mit Schreibrecht Code aus dem Root-Repo auszufuehren; das Release auf GitHub ist eine Notiz ohne Dateien. Der Workflow `image-gateway.yml` und
  `release.pub` entfallen (das Werks-Image baut kuenftig ebenfalls der Betreiber lokal). Das Gate prueft
  `gateway/host/release-roots.pem` auf den Platzhalter.
- `min_updater_version` der Bundles ist 0.8.0 (neues Manifest-Format, aeltere Updater lehnen es als `updater_zu_alt` ab).
  Der Erststart laedt mit demselben `docker load` mit Frist (`bundles.docker_load`).

## 0.7.0

- `certifi` 2026.10.19 statt 2026.7.22 (Sicherheitsmeldung GHSA-6q48-6c7w-9gw2: kompromittierte emSign-Root-CAs im
  Zertifikatsbündel; der Image-Scan meldete sie als CRITICAL).
- Diagnose am Gerät (L4-18, Labor 2026-10-20): `iproute2` (`ss`, `ip`) ist im Image und wird von `install.sh` verlangt;
  `vcgencmd` kommt über `raspi-utils` im Image (Raspberry-Pi-Archiv, `rootfs_checks.sh` prüft alle drei; `install.sh` auf
  einem fremden Debian verlangt es nicht). Das Journal schreibt alle 30 s (`SyncIntervalSec=30s`, `Storage=persistent`
  in `journald.conf.d/smartheat.conf`), nach hartem Stromverlust fehlen höchstens die letzten 30 s. Die Container
  loggen über den Treiber `journald` (`tag` = Containername `smartheat-<Dienst>-1`) statt `json-file`: `docker logs` scheitert nicht mehr an einer
  beschädigten Logdatei, und Größe und Rotation gelten für Host und Container gemeinsam. Mit dem nächsten Bundle werden
  die Container neu erzeugt (`compose up -d`). Lesen: `docker compose -p smartheat logs <Dienst>` oder
  `journalctl CONTAINER_NAME=smartheat-<Dienst>-1`.
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
- Die Laufzeit wartet beim Start bis zu 60 s (bisher 10 s) auf die Geräteliste von Zigbee2MQTT. Nach einem Stromausfall
  veröffentlicht Zigbee2MQTT sie erst nach Node-Start und Stick; mit 10 s fehlte danach die Batterie-Überwachung bis zum
  nächsten Neustart der Laufzeit, und ein W100 galt als Thermostat ohne Profil (BR-GW-1). Die Wartezeit endet mit der
  ersten Liste; nur bei abgeschaltetem Zigbee2MQTT läuft sie voll ab.
- Die Diagnoseseite schließt jede Verbindung nach spätestens 10 s (auch bei tröpfelnden Anfragen) und bedient höchstens
  64 Verbindungen gleichzeitig; weitere werden sofort geschlossen. Bisher konnte ein Gerät im LAN mit stummen
  Verbindungen Fäden und Speicher des Prozesses füllen (BR-GW-3).
- Compose-Healthcheck und Updater fragen einen eigenen Gesundheitsserver (nur `/healthz`, Port 8081 im Container, nur auf
  `127.0.0.1:8081` des Hosts veröffentlicht, eigene Plätze) statt der Diagnoseseite auf Port 80. Belegt ein Gerät im LAN
  alle Plätze der Diagnoseseite, bleiben dem Gesundheitsserver seine. Mit Docker vor Version 28 kann ein Gast im selben
  Netz veröffentlichte Ports trotzdem über direktes Routing auf das Container-Netz erreichen (die Firewall des Hosts
  sieht weitergeleiteten Verkehr nicht); das bleibt offen bis zur Härtung der Host-Firewall und einer Probe am Gerät.
  Der Updater fragt `http://127.0.0.1:8081/healthz`; `/healthz` auf Port 80 bleibt für die Diagnose.
- Scheitert beim Abmelden das Zurücksetzen der Heizung, nennt die Meldung die Werte, die das Gateway selbst geschrieben
  hätte (Ursprungswert des Servers, auf die lokalen Grenzen gebracht), nicht den lokalen Ursprungswert (BR-KK-2, Client-Kern
  für beide Hosts).
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
