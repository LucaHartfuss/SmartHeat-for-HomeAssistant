# Ausnahmen fuer vulture (scripts/check.sh, Schritt lint, K4), Whitelist-Format von vulture.
# vulture zaehlt jeden Namen hier als benutzt. Jede Zeile nennt den Grund; keine Zeile ohne Grund. Der Lauf nimmt
# heizungsbruecke/src mit (der Kern ruft die Treiber- und Host-Methoden des Gateways ueber seine Protocols) und deren
# Ausnahmen (heizungsbruecke/vulture_ausnahmen.py).

# --- Befehls-Handler: Decorator @_handler traegt sie in befehle.HANDLERS ein (Register des Geraetekerns) ---
_.zigbee_permit_join  # smartheat_gateway/befehle.py
_.zigbee_devices  # smartheat_gateway/befehle.py
_.driver_login  # smartheat_gateway/befehle.py
_.sign_off  # smartheat_gateway/befehle.py

# --- paho-mqtt: Callback-Attribute und Callback-Signaturen (lokaler Bus, CallbackAPIVersion.VERSION2) ---
_.on_connect  # smartheat_gateway/bus.py: paho ruft client.on_connect
_.on_disconnect  # smartheat_gateway/bus.py: paho ruft client.on_disconnect
_.on_message  # smartheat_gateway/bus.py: paho ruft client.on_message
_.userdata  # smartheat_gateway/bus.py: Parameter der paho-Callback-Signatur

# --- http.server: BaseHTTPRequestHandler ruft die Methoden ueber ihren Namen ---
_.do_GET  # smartheat_gateway/diagnose.py: Antwort auf GET
_.do_POST  # smartheat_gateway/diagnose.py: Antwort auf POST (nur /anmelden)
_.log_message  # smartheat_gateway/diagnose.py: ueberschreibt das Zugriffslog (still)
_.send_response  # smartheat_gateway/diagnose.py: ueberschreibt die Antwort ohne Server-Kopf (kein Banner, S-13)
_.send_error  # smartheat_gateway/diagnose.py: ueberschreibt die Fehlerseite der Basisklasse (nennt Methode und Version)
_.explain  # smartheat_gateway/diagnose.py: Parameter der send_error-Signatur der Basisklasse
_.server_version  # smartheat_gateway/diagnose.py: leer, die Basisklasse liest es fuer den Server-Kopf (S-13)
_.sys_version  # smartheat_gateway/diagnose.py: leer, wie server_version
_.close_connection  # smartheat_gateway/diagnose.py: Attribut der Basisklasse (keine Weiterverwendung der Verbindung)
_.request_queue_size  # smartheat_gateway/diagnose.py: Attribut von socketserver.TCPServer (Rueckstau des Listen-Sockets)

# --- Bespiel-Station (AP-05b) und Tests: erzeugt das Feld passwort von werk.json ---
_.passwort_eintrag  # smartheat_gateway/anmeldung.py: Aufrufer sind die Station (AP-05b) und gateway/tests/test_anmeldung.py

# --- Signalhandler: signal.signal ruft handler(signum, frame) ---
_.signum  # smartheat_gateway/runtime_main.py _terminate
_.frame  # smartheat_gateway/runtime_main.py _terminate

# --- Objekt mit Wirkung im Konstruktor ---
_._eingang  # smartheat_gateway/triggers.py: der SollEingang registriert sich beim Worker; das Attribut haelt ihn
