# Ausnahmen fuer vulture (scripts/check.sh, Schritt lint; Stufe 3 S8b, A5-87, K4), Whitelist-Format von vulture.
# vulture zaehlt jeden Namen hier als benutzt. Jede Zeile nennt den Grund; keine Zeile ohne Grund. Vertragskonstanten
# stehen nicht hier, sondern als offene Befunde in vulture_vertragsbefunde.py.
# Gilt fuer heizungsbruecke/src (HA-Host, Kern, Laufzeit, Geraetekern, Transport); der Lauf nimmt gateway/src mit, weil
# das Gateway auf demselben Kern aufbaut.

# --- Signalhandler: signal.signal ruft handler(signum, frame) ---
_.signum  # heizungsbruecke/__main__.py _terminate
_.frame  # heizungsbruecke/__main__.py _terminate

# --- paho-mqtt: Callback-Attribute und Callback-Signaturen (CallbackAPIVersion.VERSION2) ---
_.on_connect  # smartheat_device/link.py: paho ruft client.on_connect
_.on_disconnect  # smartheat_device/link.py: paho ruft client.on_disconnect
_.on_connect_fail  # smartheat_device/link.py: paho ruft client.on_connect_fail
_.on_message  # smartheat_device/link.py: paho ruft client.on_message
_.on_publish  # smartheat_device/link.py: paho ruft client.on_publish
_.SessionExpiryInterval  # smartheat_device/link.py: MQTT-5-Property, paho liest sie beim connect
_.userdata  # smartheat_device/link.py: Parameter der paho-Callback-Signatur

# --- Testeinstiege mit Namen (Tests und Szenarien treiben den Code ohne Threads) ---
_.schritt  # smartheat_device/device.py Device.schritt: ein Durchlauf ohne Warten (gateway/tests/world.py, Szenarien)
_.run_pending  # smartheat_runtime/worker.py RegulationWorker.run_pending: Takt mit Fake-Uhr (Tests im Add-on und Gateway)
_.kinds  # smartheat_device/commands.py CommandRegister.kinds: registrierte Befehlsarten (test_geraet_host, test_befehle)

# --- testgebunden, Regel-4-Dateien (nicht loeschen) ---
_.verteilsysteme  # smartheat_core/safety.py: Verteilsysteme der lokalen Sicherheitswerte; test_core_safety pinnt die
#                   Tabelle LOCAL_SAFETY ueber alle Hebelsaetze x Verteilsysteme (Spiegel: Server-Test test_profiles)
_.MAX_WRITES_PER_DAY  # smartheat_core/enforce.py: Tagesgrenze des Durchsetzens (= write_budget.MAX_PER_DAY); die
#                       Tests test_core_enforce und test_runtime zaehlen die Schreibvorgaenge dagegen
_.QUOTA  # smartheat_core/write_budget.py: Schreibgrenze des Durchsetzens (1/30 min, 6/Tag); nur
#          test_core_write_budget liest sie (das QUOTA des ViCare-Treibers ist ein anderer Name, gleich geschrieben)
