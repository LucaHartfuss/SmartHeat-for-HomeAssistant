#!/bin/sh
set -e

OPTIONS_FILE="${OPTIONS_FILE_OVERRIDE:-/data/options.json}"

# `// empty`: ein fehlender Schluessel ergibt einen leeren Text statt "null" (die Leerpruefung unten greift).
HOSTNAME=$(jq -r '.hostname // empty' "$OPTIONS_FILE")
LOCAL_PORT=$(jq -r '.local_port // empty' "$OPTIONS_FILE")
SERVICE_TOKEN_ID=$(jq -r '.service_token_id // empty' "$OPTIONS_FILE")
SERVICE_TOKEN_SECRET=$(jq -r '.service_token_secret // empty' "$OPTIONS_FILE")

if [ -z "$HOSTNAME" ] || [ -z "$LOCAL_PORT" ] || [ -z "$SERVICE_TOKEN_ID" ] || [ -z "$SERVICE_TOKEN_SECRET" ]; then
  echo "FEHLER: hostname, local_port, service_token_id und service_token_secret muessen in der Add-on-Konfiguration gesetzt sein." >&2
  exit 1
fi

# AU-034: Das Token steht nicht in der argv (per ps im Host-Namespace sichtbar), sondern in der
# Umgebung. Die Namen sind gegen cloudflared 2025.8.1 geprueft (cmd/cloudflared/access/cmd.go:
# --service-token-id = TUNNEL_SERVICE_TOKEN_ID, --service-token-secret = TUNNEL_SERVICE_TOKEN_SECRET,
# nur am Kommando `access tcp`). Beim Wechsel der cloudflared-Version erneut pruefen.
export TUNNEL_SERVICE_TOKEN_ID="$SERVICE_TOKEN_ID"
export TUNNEL_SERVICE_TOKEN_SECRET="$SERVICE_TOKEN_SECRET"

exec cloudflared access tcp \
  --hostname "$HOSTNAME" \
  --url "127.0.0.1:${LOCAL_PORT}"
