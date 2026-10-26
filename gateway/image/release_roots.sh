#!/bin/bash
# Pruefung fuer --release-roots (Labor-Images), von prepare.sh und make_image.sh eingebunden
# (nicht im Installer-Baum): eine vorhandene Datei mit dem Schluesselbund der Roots im PEM-Format, wie
# gateway/host/smartheat_host/kette.py (load_roots) ihn liest - ein oder mehrere Zertifikatsbloecke
# "-----BEGIN CERTIFICATE-----" ... "-----END CERTIFICATE-----" mit Base64-Zeilen dazwischen, sonst nur Leerzeilen.
# Das ist nur die Form (kein Text davor oder danach, keine Schluessel, kein Platzhalter); ob jede Root eine CA mit
# P-256-Schluessel ist, prueft der Updater beim Laden (kette.load_roots). Gibt 0 zurueck oder 1; der Aufrufer
# meldet den Fehler.
shg_release_roots_ok() {
  local LC_ALL=C file="$1" line state=0 body=0 count=0
  [ -f "$file" ] || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    if [ "$state" = 0 ]; then
      [ -z "$line" ] && continue
      [ "$line" = "-----BEGIN CERTIFICATE-----" ] || return 1
      state=1 body=0
    elif [ "$line" = "-----END CERTIFICATE-----" ]; then
      [ "$body" -gt 0 ] || return 1
      state=0 count=$((count + 1))
    elif [[ $line =~ ^[A-Za-z0-9+/]+=*$ ]]; then
      body=$((body + 1))
    else
      return 1
    fi
  done <"$file"
  [ "$state" = 0 ] && [ "$count" -ge 1 ]
}
shg_release_roots_hint="--release-roots: eine vorhandene Datei mit dem Schluesselbund der Roots (PEM, ein oder mehrere Bloecke 'BEGIN CERTIFICATE' bis 'END CERTIFICATE') erwartet"
