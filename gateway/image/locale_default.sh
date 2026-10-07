#!/bin/bash
# customize-Hook des Layers smartheat-gateway: Standard-Locale des Images (Nutzer-Vorgabe 2026-10-07). rpi-image-gen
# v2.8.0 belegt mit locale.default (config/smartheat-gateway.yaml) nur debconf vor: locales erzeugt die Locale
# (/etc/locale.gen, locale-archive), der essential-Hook von locale-base schreibt aber LANG=C.UTF-8 nach
# /etc/locale.conf, und das Einrichten von locales laesst eine vorhandene Datei unveraendert (geprueft mit dem Paket
# locales 2.41 aus trixie). Dieser Hook setzt LANG per update-locale im chroot; update-locale bricht ab, wenn die
# Locale nicht erzeugt ist. --reset: nur LANG bleibt wirksam (LANGUAGE=C von locale-base wird auskommentiert).
# Aufruf (mmdebstrap): locale_default.sh ROOTFS, Umgebung IGconf_locale_default (z. B. de_DE.UTF-8).
set -euo pipefail
R="${1:?Root-Dateisystem fehlt}"
loc="${IGconf_locale_default:-}"
if [[ ! $loc =~ ^[a-z]{2,3}_[A-Z]{2}\.UTF-8$ ]]; then
  echo "locale_default.sh: locale.default muss eine UTF-8-Locale wie de_DE.UTF-8 sein, nicht '${loc}'" >&2
  exit 1
fi
chroot "$R" update-locale --reset "LANG=$loc"
echo "Standard-Locale: LANG=$loc"
