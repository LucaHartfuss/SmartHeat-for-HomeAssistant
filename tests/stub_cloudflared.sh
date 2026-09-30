#!/bin/sh
# Fake-cloudflared fuer Tests: protokolliert seine Argumente (STUB_LOG) und die Token-Umgebung (STUB_ENV_LOG).
: > "$STUB_LOG"
for arg in "$@"; do
  printf '%s\n' "$arg" >> "$STUB_LOG"
done
if [ -n "${STUB_ENV_LOG:-}" ]; then
  {
    printf 'TUNNEL_SERVICE_TOKEN_ID=%s\n' "${TUNNEL_SERVICE_TOKEN_ID-}"
    printf 'TUNNEL_SERVICE_TOKEN_SECRET=%s\n' "${TUNNEL_SERVICE_TOKEN_SECRET-}"
  } > "$STUB_ENV_LOG"
fi
