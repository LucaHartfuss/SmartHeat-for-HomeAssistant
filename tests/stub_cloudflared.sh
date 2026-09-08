#!/bin/sh
# Fake-cloudflared fuer Tests: protokolliert nur seine Argumente, tut sonst nichts.
: > "$STUB_LOG"
for arg in "$@"; do
  printf '%s\n' "$arg" >> "$STUB_LOG"
done
