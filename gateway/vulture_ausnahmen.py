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
_.log_message  # smartheat_gateway/diagnose.py: ueberschreibt das Zugriffslog (still)

# --- Signalhandler: signal.signal ruft handler(signum, frame) ---
_.signum  # smartheat_gateway/runtime_main.py _terminate
_.frame  # smartheat_gateway/runtime_main.py _terminate

# --- Objekt mit Wirkung im Konstruktor ---
_._eingang  # smartheat_gateway/triggers.py: der SollEingang registriert sich beim Worker; das Attribut haelt ihn
