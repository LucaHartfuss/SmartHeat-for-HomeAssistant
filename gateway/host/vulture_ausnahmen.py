# Ausnahmen fuer vulture (scripts/check.sh, Schritt lint, K4), Whitelist-Format von vulture.
# vulture zaehlt jeden Namen hier als benutzt. Jede Zeile nennt den Grund; keine Zeile ohne Grund.

# --- Signalhandler: signal.signal ruft handler(signum, frame) ---
_.signum  # smartheat_host/led.py _terminate
_.frame  # smartheat_host/led.py _terminate

# --- urllib liest OpenerDirector.addheaders bei jeder Anfrage (auch nach einer Weiterleitung) ---
_.addheaders  # smartheat_host/updater.py checked_opener (eigener User-Agent)

# --- urllib ruft <schema>_open des Handlers fuer jede Anfrage (auch fuer ein Weiterleitungsziel) ---
_.https_open  # smartheat_host/updater.py _FetchHTTPSHandler
_.http_open  # smartheat_host/updater.py _FetchHTTPHandler (nur mit allow_http)
_.recv_into  # smartheat_host/updater.py _DeadlineSocket, DeadlineSSLSocket (Frist je Leseoperation): SocketIO ruft es

# --- Kennung des Schluesselbunds: Aufrufer (Updater, Statusmeldung) kommen mit den Folge-Tasks von AP-04; getestet ---
_.roots_identity  # smartheat_host/kette.py roots_identity (tests/test_kette.py)
