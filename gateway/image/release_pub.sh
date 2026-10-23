#!/bin/bash
# Pruefung fuer --release-pub (Labor-Images), von prepare.sh und make_image.sh eingebunden
# (nicht im Installer-Baum): eine vorhandene Datei mit einem oeffentlichen minisign-Schluessel, genau wie ihn
# gateway/host/smartheat_host/minisign.py (load_public_key) liest - Zeile "untrusted comment: ..." und Base64 von 42
# Byte ("Ed" + Schluessel-ID + Schluessel), also 56 Zeichen, die mit RW beginnen. Gibt 0 zurueck oder 1; der Aufrufer
# meldet den Fehler.
shg_release_pub_ok() {
  local file="$1" lines
  [ -f "$file" ] || return 1
  mapfile -t lines < <(grep -v '^[[:space:]]*$' "$file" || true)
  [ ${#lines[@]} -eq 2 ] || return 1
  [[ ${lines[0]} == "untrusted comment:"* ]] || return 1
  [[ ${lines[1]} =~ ^RW[A-Za-z0-9+/]{54}$ ]]
}
shg_release_pub_hint="--release-pub: eine vorhandene Datei mit einem oeffentlichen minisign-Schluessel erwartet (Zeile 'untrusted comment: ...' und Schluesselzeile 'RW...' mit 56 Zeichen)"
