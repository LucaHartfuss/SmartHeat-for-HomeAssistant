# Ausnahmen fuer vulture (scripts/check.sh, Schritt lint; Stufe 3 S8b, A5-87, K4), Whitelist-Format von vulture.
# vulture zaehlt jeden Namen hier als benutzt. Jede Zeile nennt den Grund; keine Zeile ohne Grund.

# --- Signalhandler: signal.signal ruft handler(signum, frame) ---
_.signum  # smartheat_host/led.py _terminate
_.frame  # smartheat_host/led.py _terminate

# --- urllib liest OpenerDirector.addheaders bei jeder Anfrage (auch nach einer Weiterleitung) ---
_.addheaders  # smartheat_host/updater.py checked_opener (eigener User-Agent)
