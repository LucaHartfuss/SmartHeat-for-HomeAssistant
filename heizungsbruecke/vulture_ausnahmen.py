# Ausnahmen fuer vulture (scripts/check.sh, Schritt lint), Whitelist-Format von vulture.
# vulture zaehlt jeden Namen hier als benutzt. Jede Zeile nennt den Grund; keine Zeile ohne Grund. Vertragsnamen nur,
# wenn der Leser auf der Gegenseite der woertlichen Kopie liegt. Neue Ausnahme
# nur mit Grund, sonst den toten Code loeschen.
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

# --- Vertragskopie V1 (smartheat_device/wire.py, woertlich wie geraete/device_protocol.py des Servers): Leser im Server ---
_.MAX_CLOCK_SKEW_SECONDS  # Server: geraete/device_auth.py (Zeitfenster der Signatur); Geraet wertet nur den Status 401
_.UNAUTHENTICATED_ERROR  # Server: geraete/devices_api.py (Text der 401-Antwort); das Geraet wertet nur den Status 401
_.ENERGIE_EINHEITEN  # Server: einrichtung/ha_pruefung.py, einrichtung/ha.py, geraete/device_config.py; im Geraet nur gepinnt (bindungsparameter.EINHEITEN)
_.expires_rule  # Feld von wire.Command: Server geraete/device_protocol.py wertet die Regel aus (expires_seconds None)
