#!/bin/bash
# Gemeinsame Hilfen der Gateway-Docker-Pruefungen (Plan 5b D2 Task 10): Test-PKI, Fake-Broker (Mosquitto mit TLS und
# Client-Zertifikat, ACL wie die Geraete-Policy) und die Dienst-Rolle (CN dienst), die Dokumente sendet und Ergebnisse
# liest. Wird per "source" eingebunden; erwartet MOSQ_IMAGE und BROKER (Container- bzw. Dienstname des Fake-Brokers,
# zugleich SAN des Broker-Zertifikats). Die Test-PKI entsteht je Lauf neu im Datenordner des Tests und wird nie
# eingecheckt (Regel 6). Ihre Dateien gehoeren dem Benutzer des Skripts (0600, Ordner 0700); alle Container, die sie
# lesen (Fake-Broker, Fake-Bootstrap, Dienst-Rolle), laufen deshalb als PKI_USER. So liest Mosquitto eigene Dateien
# ohne Rechte fuer andere (sonst warnt Mosquitto 2.1 und kuenftige Versionen verweigern die ACL-Datei).
PKI_USER="$(id -u):$(id -g)"

# make_pki DIR BROKER_NAME: ca.pem/ca.key, broker.pem/broker.key (SAN DNS:BROKER_NAME), dienst.pem/dienst.key.
make_pki() {
  local dir=$1 broker=$2 name cn
  mkdir -p "$dir" || return 1
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout "$dir/ca.key" -out "$dir/ca.pem" \
    -days 2 -subj "/CN=shg-test-ca" 2>/dev/null || return 1
  for name in broker dienst; do
    if [ "$name" = broker ]; then cn=$broker; else cn=dienst; fi
    openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout "$dir/$name.key" -out "$dir/$name.csr" \
      -subj "/CN=$cn" 2>/dev/null || return 1
  done
  printf 'subjectAltName=DNS:%s\nextendedKeyUsage=serverAuth\n' "$broker" >"$dir/broker.ext"
  printf 'extendedKeyUsage=clientAuth\n' >"$dir/dienst.ext"
  for name in broker dienst; do
    openssl x509 -req -in "$dir/$name.csr" -CA "$dir/ca.pem" -CAkey "$dir/ca.key" -CAcreateserial -days 2 \
      -extfile "$dir/$name.ext" -out "$dir/$name.pem" 2>/dev/null || return 1
  done
  rm -f "$dir"/*.csr "$dir"/*.ext "$dir"/*.srl
  chmod 0700 "$dir" && chmod 0600 "$dir"/*.pem "$dir"/*.key
}

# broker_conf DIR: mosquitto.conf und acl fuer den Fake-Broker (Geraete-Policy, Spec 5b 4.1; Dienst darf alles).
broker_conf() {
  local dir=$1
  cat >"$dir/mosquitto.conf" <<'EOF' || return 1
listener 8883
cafile /pki/ca.pem
certfile /pki/broker.pem
keyfile /pki/broker.key
require_certificate true
use_identity_as_username true
acl_file /pki/acl
persistence false
log_dest stdout
log_type error
log_type warning
log_type notice
log_type information
EOF
  cat >"$dir/acl" <<'EOF' || return 1
user dienst
topic readwrite smartheat/#

pattern write smartheat/%u/up/#
pattern write smartheat/%u/telemetry
pattern read smartheat/%u/down/#
EOF
  chmod 0600 "$dir/mosquitto.conf" "$dir/acl"
}

# dienst_pub NET DIR TOPIC JSON: als Dienst QoS 1 veroeffentlichen.
dienst_pub() {
  docker run --rm --security-opt label=disable --user "$PKI_USER" --network "$1" -v "$2:/pki:ro" "$MOSQ_IMAGE" \
    mosquitto_pub -h "$BROKER" -p 8883 --cafile /pki/ca.pem --cert /pki/dienst.pem --key /pki/dienst.key \
    -q 1 -t "$3" -m "$4"
}

# dienst_wait NET DIR TOPIC_FILTER SEKUNDEN: erste Nachricht (Payload) oder leer nach der Frist.
dienst_wait() {
  docker run --rm --security-opt label=disable --user "$PKI_USER" --network "$1" -v "$2:/pki:ro" "$MOSQ_IMAGE" \
    mosquitto_sub -h "$BROKER" -p 8883 --cafile /pki/ca.pem --cert /pki/dienst.pem --key /pki/dienst.key \
    -q 1 -t "$3" -C 1 -W "$4" 2>/dev/null
}
