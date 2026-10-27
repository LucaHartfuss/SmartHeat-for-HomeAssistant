#!/bin/bash
# setup-Hook des Layers smartheat-gateway: Docker-Quelle (docker-apt/docker.sources, im Layer als Mirror) vertrauen und
# die Pakete auf docker-apt/version festnageln (FR-34). Der Schluessel docker.asc liegt im Repo; hier wird sein
# Fingerabdruck gegen den in docker-apt/version geprueft, bevor apt ihm vertraut. Der Schluessel kommt in zwei Orte wie
# im Docker-Layer von rpi-image-gen: ins Root-Dateisystem (/usr/share/keyrings/docker.asc, dort steht X-IG-Signed-By
# der Quelle) und in das Schluesselverzeichnis, dem das apt von mmdebstrap beim Bau vertraut (IGconf_sys_apt_keydir).
# Die Versionen schreibt der Hook als apt-Pin (Prioritaet 1001: auch gegen eine neuere Version im Index) nach
# /etc/apt/preferences.d/smartheat-docker.pref; der Pin bleibt im Image.
# Aufruf (mmdebstrap setup-hook): docker_apt.sh ROOTFS   Umgebung: IGconf_sys_apt_keydir (Verzeichnis, darf fehlen)
set -euo pipefail
R="${1:?Root-Dateisystem fehlt}"
HERE="$(cd "$(dirname "$0")" && pwd)"
DIR="$HERE/docker-apt"
KEY="$DIR/docker.asc"
fail() { echo "docker_apt.sh: $1" >&2; exit 1; }
[ -f "$KEY" ] && [ -f "$DIR/version" ] || fail "docker-apt/docker.asc oder docker-apt/version fehlt"

# docker-apt/version: Name=Wert, Kommentare mit #. Doppelte Namen sind ein Fehler (der Pin waere mehrdeutig).
declare -A V=()
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in ''|'#'*) continue ;; esac
  [[ $line =~ ^([a-z0-9.+-]+)=([^[:space:]]+)$ ]] || fail "docker-apt/version: unlesbare Zeile: $line"
  [ -z "${V[${BASH_REMATCH[1]}]+x}" ] || fail "docker-apt/version: ${BASH_REMATCH[1]} doppelt"
  V[${BASH_REMATCH[1]}]=${BASH_REMATCH[2]}
done <"$DIR/version"
PACKAGES=(docker-ce docker-ce-cli containerd.io docker-compose-plugin)
for pkg in fingerprint "${PACKAGES[@]}"; do
  [ -n "${V[$pkg]:-}" ] || fail "docker-apt/version: $pkg fehlt"
done
[[ ${V[fingerprint]} =~ ^[0-9A-F]{40}$ ]] || fail "docker-apt/version: fingerprint ist kein Fingerabdruck (40 Hex, gross)"

# Der erste fpr-Eintrag gehoert zum Hauptschluessel (danach die Unterschluessel). Genau ein Hauptschluessel.
mapfile -t keys < <(gpg --batch --quiet --show-keys --with-colons --fingerprint "$KEY" | awk -F: '$1 == "pub" { getline; print $10 }')
[ ${#keys[@]} -eq 1 ] || fail "docker-apt/docker.asc enthaelt ${#keys[@]} Schluessel (genau einer erwartet)"
[ "${keys[0]}" = "${V[fingerprint]}" ] || fail "Fingerabdruck von docker.asc (${keys[0]}) weicht von docker-apt/version ab"

install -D -m 0644 "$KEY" "$R/usr/share/keyrings/docker.asc"
if [ -n "${IGconf_sys_apt_keydir:-}" ]; then install -D -m 0644 "$KEY" "$IGconf_sys_apt_keydir/docker.asc"; fi
mkdir -p "$R/etc/apt/preferences.d"
{
  echo "# SmartHeat-Gateway: Docker-Pakete auf docker-apt/version festgenagelt (gateway/image/docker_apt.sh)"
  for pkg in "${PACKAGES[@]}"; do
    printf '\nPackage: %s\nPin: version %s\nPin-Priority: 1001\n' "$pkg" "${V[$pkg]}"
  done
} >"$R/etc/apt/preferences.d/smartheat-docker.pref"
echo "Docker-Quelle: Schluessel ${V[fingerprint]} geprueft, ${#PACKAGES[@]} Pakete festgenagelt (docker-ce ${V[docker-ce]})"
