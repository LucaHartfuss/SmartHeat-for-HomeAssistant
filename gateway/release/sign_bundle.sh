#!/usr/bin/env bash
# Signiert manifest.json eines Bundles mit minisign und prueft die Signatur mit dem Code des Geraets
# (smartheat_host.minisign). Ein Skript fuer den echten Release und den -dryrun (Spec G2b-1 7), damit der Probelauf
# denselben Pruef-, Signier- und Pruefweg geht. Vor dem Signieren baut verify_bundle.py das Bundle aus diesem Checkout
# (dem Tag) mit dem Gateway-Digest aus dem Manifest neu und vergleicht byte-genau; images.gateway muss aus
# ghcr.io/lucahartfuss/smartheat-gateway stammen (--dryrun: aus der Job-Registry localhost:5000/smartheat-gateway).
#
# Aufruf:  sign_bundle.sh BUNDLE_DIR VERSION PUBLIC_KEY [--dryrun]
#   echt:    Schluessel aus $MINISIGN_SECRET_KEY (Base64 der Schluesseldatei) und $MINISIGN_PASSWORD; PUBLIC_KEY ist
#            der vorhandene oeffentliche Schluessel (gateway/host/release.pub), gegen den geprueft wird.
#   --dryrun: erzeugt einen Wegwerf-Schluessel mit Passwort ("test-pw"), schreibt dessen oeffentlichen Teil nach
#            PUBLIC_KEY und liest keine Secrets.
# minisign liest das Passwort ohne Terminal von stdin (minisign 0.11, Ubuntu 24.04). Der geheime Schluessel liegt nur
# kurz in einem 0700-Verzeichnis und wird beim Beenden (auch bei Fehlern) geloescht. Nichts davon geht in die Ausgabe.
# Python (Repo-Code und pip-Pakete) laeuft nie mit den Secrets im Environment (env -u).
set -euo pipefail
umask 077
if [ "$#" -lt 3 ] || [ "$#" -gt 4 ]; then
  echo "Aufruf: $0 BUNDLE_DIR VERSION PUBLIC_KEY [--dryrun]" >&2
  exit 2
fi
bundle="$1"; version="$2"; pub="$3"; mode="${4:-}"
if [ -n "$mode" ] && [ "$mode" != "--dryrun" ]; then
  echo "Unbekannte Option: $mode" >&2
  exit 2
fi
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$here/../host:$here/../src"
image_repo="ghcr.io/lucahartfuss/smartheat-gateway"
[ "$mode" = "--dryrun" ] && image_repo="localhost:5000/smartheat-gateway"
py() { env -u MINISIGN_SECRET_KEY -u MINISIGN_PASSWORD "${PYTHON:-python}" "$@"; }
rm -f "$bundle/manifest.json.minisig"
# Vor dem Signieren: das Bundle kommt aus einem anderen Job (Artefakt) und ist Daten, nicht vertrauenswuerdig.
py "$here/verify_bundle.py" "$bundle" "$version" --image-repo "$image_repo"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
key="$work/release.key"
if [ "$mode" = "--dryrun" ]; then
  password="test-pw"
  printf '%s\n%s\n' "$password" "$password" | minisign -G -p "$pub" -s "$key" >/dev/null
else
  if [ -z "${MINISIGN_SECRET_KEY:-}" ] || [ -z "${MINISIGN_PASSWORD:-}" ]; then
    echo "::error::MINISIGN_SECRET_KEY/MINISIGN_PASSWORD fehlen im Environment 'release' (Runbook Gateway-Release)"
    exit 1
  fi
  printf '%s' "$MINISIGN_SECRET_KEY" | base64 -d > "$key"
  password="$MINISIGN_PASSWORD"
fi
printf '%s\n' "$password" | minisign -S -s "$key" -m "$bundle/manifest.json" -x "$bundle/manifest.json.minisig" \
  -t "smartheat-gateway $version" >/dev/null
rm -f "$key"
py -m smartheat_host.minisign "$pub" "$bundle/manifest.json" "$bundle/manifest.json.minisig"
