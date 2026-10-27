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

# --- Startauswahl: Schnittstelle fuer den Updater (AP-05a Task 3), der sie ruft; bis dahin ohne Leser im Paket.
# Task 3 streicht diese Zeilen, sobald updater.py die Namen benutzt. ---
Startauswahl  # smartheat_host/startauswahl.py (Protocol, Typ des Updaters)
RaucTryboot  # smartheat_host/startauswahl.py (echte Startauswahl, main des Updaters)
_.im_probestart  # smartheat_host/startauswahl.py Startauswahl, RaucTryboot
_.compatible  # smartheat_host/startauswahl.py Startauswahl, RaucTryboot
_.installieren  # smartheat_host/startauswahl.py Startauswahl, RaucTryboot
_.probestart  # smartheat_host/startauswahl.py Startauswahl, RaucTryboot
_.bestaetigen  # smartheat_host/startauswahl.py Startauswahl, RaucTryboot
_.zurueckschalten  # smartheat_host/startauswahl.py Startauswahl, RaucTryboot
