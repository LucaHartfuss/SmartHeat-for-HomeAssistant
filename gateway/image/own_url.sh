# shellcheck shell=bash
# Regel fuer Geraete-API- und Portal-Adresse im Gateway-Image (Plan G2b-2 Task 9, Nutzer-Vorgabe 2026-10-07): nur
# https://<eigener DNS-Name>[/] - nur ASCII, keine IP-Adresse (v4/v6), kein Name ohne Punkt, keine AWS-Adresse
# (amazonaws.com/.cn, awsapprunner.com, amazonlightsail.com, amazoncognito.com, awsapps.com, cloudfront.net, .aws, ...),
# keine fremden Platzhalter- oder privaten Namen (nip.io, sslip.io, xip.io, .internal, .home.arpa, .local, .localdomain,
# .lan, localhost, Reverse-DNS), keine Zugangsdaten (@), kein Port, kein Pfad, keine Query. So zeigt jedes Geraet auf
# einen Namen, den wir selbst umziehen koennen (AWS-Umzug ohne neues Image). Einzige Quelle der Regel: per source
# eingebunden von prepare.sh, make_image.sh und rootfs_checks.sh. Unabhaengig von der Locale des Aufrufers (LC_ALL=C nur
# in der Funktion): unter UTF-8-Locales traefen Bereiche wie [a-z] sonst auch Nicht-ASCII (Vollbreite, Umlaute).
#
# shg_own_url_problem URL: Exit 0 ohne Ausgabe, wenn URL zulaessig ist; sonst Exit 1 und der Grund auf stdout.
shg_own_url_problem() {
  local LC_ALL=C
  local url=${1-} host label suffix
  local -a labels
  if [ -z "$url" ]; then echo "fehlt"; return 1; fi
  case "$url" in https://*) ;; *) echo "muss mit https:// beginnen"; return 1 ;; esac
  host=${url#https://}
  case "$host" in */?*) echo "Pfad, Query oder Fragment nicht erlaubt"; return 1 ;; esac
  host=${host%/}
  case "$host" in
    *@*) echo "Zugangsdaten (@) nicht erlaubt"; return 1 ;;
    *'['*|*']'*) echo "IPv6-Adresse nicht erlaubt"; return 1 ;;
    *:*) echo "Port nicht erlaubt"; return 1 ;;
    *'?'*|*'#'*) echo "Query oder Fragment nicht erlaubt"; return 1 ;;
  esac
  host=${host,,}
  if [ ${#host} -gt 253 ] || [[ ! $host =~ ^[a-z0-9.-]+$ ]]; then echo "kein gueltiger DNS-Name"; return 1; fi
  IFS=. read -r -a labels <<<"$host"
  if [ ${#labels[@]} -lt 2 ] || [ "${host: -1}" = . ] || [ "${host:0:1}" = . ]; then
    echo "kein vollstaendiger DNS-Name (mindestens zwei Teile, z. B. accounts.example.org)"; return 1
  fi
  for label in "${labels[@]}"; do
    if [[ ! $label =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]]; then echo "kein gueltiger DNS-Name"; return 1; fi
  done
  # Die oberste Ebene beginnt mit einem Buchstaben: schliesst IPv4-Adressen in jeder Schreibweise (1.2.3.4, 127.1) aus.
  if [[ ! ${labels[-1]} =~ ^[a-z] ]]; then echo "IP-Adresse nicht erlaubt, nur ein eigener DNS-Name"; return 1; fi
  case "$host" in
    *amazonaws.com*|*amazonaws.cn*|*awsapprunner.com*|*amazonlightsail.com*|*amazoncognito.com*|*awsapps.com*|*.aws)
      echo "AWS-Adresse nicht erlaubt, nur ein eigener DNS-Name"; return 1 ;;
  esac
  for suffix in cloudfront.net awsglobalaccelerator.com elasticbeanstalk.com amplifyapp.com; do
    if [ "$host" = "$suffix" ] || [[ $host == *".$suffix" ]]; then
      echo "AWS-Adresse nicht erlaubt, nur ein eigener DNS-Name"; return 1
    fi
  done
  for suffix in localhost local localdomain lan internal home.arpa in-addr.arpa ip6.arpa nip.io sslip.io xip.io; do
    if [ "$host" = "$suffix" ] || [[ $host == *".$suffix" ]]; then
      echo "lokaler oder fremder Platzhalter-Name nicht erlaubt, nur ein eigener DNS-Name"; return 1
    fi
  done
  return 0
}

# shg_require_own_url OPTION URL: Abbruch mit Exit 2 und Meldung auf stderr, wenn URL nicht zulaessig ist (fuer die
# Optionen von prepare.sh und make_image.sh).
shg_require_own_url() {
  local why
  if ! why="$(shg_own_url_problem "${2-}")"; then
    echo "FEHLER: $1 '${2-}': $why (erlaubt: https://<eigener DNS-Name>, z. B. https://accounts.hartfussha.org;" \
      "keine IP- und keine AWS-Adresse)" >&2
    exit 2
  fi
}
