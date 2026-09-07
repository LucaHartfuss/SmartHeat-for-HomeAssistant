#!/bin/sh
set -e

OPTIONS_FILE="/data/options.json"

HOSTNAME=$(jq -r '.hostname' "$OPTIONS_FILE")
LOCAL_PORT=$(jq -r '.local_port' "$OPTIONS_FILE")
SERVICE_TOKEN_ID=$(jq -r '.service_token_id' "$OPTIONS_FILE")
SERVICE_TOKEN_SECRET=$(jq -r '.service_token_secret' "$OPTIONS_FILE")

if [ -z "$HOSTNAME" ] || [ -z "$SERVICE_TOKEN_ID" ] || [ -z "$SERVICE_TOKEN_SECRET" ]; then
  echo "FEHLER: hostname, service_token_id und service_token_secret muessen in der Add-on-Konfiguration gesetzt sein." >&2
  exit 1
fi

exec cloudflared access tcp \
  --hostname "$HOSTNAME" \
  --url "127.0.0.1:${LOCAL_PORT}" \
  --service-token-id "$SERVICE_TOKEN_ID" \
  --service-token-secret "$SERVICE_TOKEN_SECRET"
