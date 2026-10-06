#!/usr/bin/env bash
# Signiert manifest.json eines Bundles mit minisign und prueft die Signatur mit dem Code des Geraets
# (smartheat_host.minisign). Ein Skript fuer den echten Release und den -dryrun (Spec G2b-1 7), damit der Probelauf
# denselben Signier- und Pruefweg geht.
#
# Aufruf:  sign_bundle.sh BUNDLE_DIR VERSION PUBLIC_KEY [--dryrun]
#   echt:    Schluessel aus $MINISIGN_SECRET_KEY (Base64 der Schluesseldatei) und $MINISIGN_PASSWORD; PUBLIC_KEY ist
#            der vorhandene oeffentliche Schluessel (gateway/host/release.pub), gegen den geprueft wird.
#   --dryrun: erzeugt einen Wegwerf-Schluessel mit Passwort ("test-pw"), schreibt dessen oeffentlichen Teil nach
#            PUBLIC_KEY und liest keine Secrets.
# minisign liest das Passwort ohne Terminal von stdin (minisign 0.11, Ubuntu 24.04). Der geheime Schluessel liegt nur
# kurz in einem 0700-Verzeichnis und wird beim Beenden (auch bei Fehlern) geloescht. Nichts davon geht in die Ausgabe.
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
rm -f "$bundle/manifest.json.minisig"
# Vor dem Signieren: das Bundle kommt aus einem anderen Job (Artefakt) und ist Daten, nicht vertrauenswuerdig.
# Signiert wird nur ein in sich stimmiges Bundle genau dieser Version (dieselben Pruefungen wie auf dem Geraet).
"${PYTHON:-python}" - "$bundle" "$version" <<'PY'
import hashlib
import sys
from pathlib import Path

from smartheat_host import bundles

bundle, version = Path(sys.argv[1]), sys.argv[2]
expected = {"manifest.json", *bundles.MANIFEST_FILES}
found = {path.name for path in bundle.iterdir()}
if found != expected:
    sys.exit(f"Bundle enthaelt {sorted(found)}, erwartet {sorted(expected)}")
manifest = bundles.parse_manifest((bundle / "manifest.json").read_bytes())
if manifest.version != version:
    sys.exit(f"Manifest-Version {manifest.version} passt nicht zur Release-Version {version}")
for name, sha in manifest.files.items():
    if hashlib.sha256((bundle / name).read_bytes()).hexdigest() != sha:
        sys.exit(f"{name}: SHA-256 passt nicht zum Manifest")
bundles.check_compose((bundle / "docker-compose.yml").read_text())
PY
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
"${PYTHON:-python}" -m smartheat_host.minisign \
  "$pub" "$bundle/manifest.json" "$bundle/manifest.json.minisig"
