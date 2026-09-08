#!/bin/bash
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
RUN_SH="$HERE/../cloudflared_access_mqtt/run.sh"
FAIL=0

assert_eq() {
  if [ "$1" != "$2" ]; then
    echo "FAIL: $3 -- expected [$2], got [$1]"
    FAIL=1
  else
    echo "PASS: $3"
  fi
}

test_full_options_invokes_cloudflared_correctly() {
  TMPDIR="$(mktemp -d)"
  DATA_DIR="$TMPDIR/data"
  STUB_DIR="$TMPDIR/bin"
  mkdir -p "$DATA_DIR" "$STUB_DIR"
  cp "$HERE/stub_cloudflared.sh" "$STUB_DIR/cloudflared"
  chmod +x "$STUB_DIR/cloudflared"

  cat > "$DATA_DIR/options.json" <<JSON
{"hostname":"test.example.com","local_port":18830,"service_token_id":"fake-id","service_token_secret":"fake-secret"}
JSON

  export STUB_LOG="$TMPDIR/stub.log"
  PATH="$STUB_DIR:$PATH" OPTIONS_FILE_OVERRIDE="$DATA_DIR/options.json" sh "$RUN_SH" \
    --options-file "$DATA_DIR/options.json" 2>"$TMPDIR/stderr.log"

  EXPECTED="access
tcp
--hostname
test.example.com
--url
127.0.0.1:18830
--service-token-id
fake-id
--service-token-secret
fake-secret"
  ACTUAL="$(cat "$STUB_LOG" 2>/dev/null || echo MISSING)"
  assert_eq "$ACTUAL" "$EXPECTED" "vollstaendige Optionen -> korrekter cloudflared-Aufruf"

  rm -rf "$TMPDIR"
}

test_missing_hostname_fails_cleanly() {
  TMPDIR="$(mktemp -d)"
  DATA_DIR="$TMPDIR/data"
  STUB_DIR="$TMPDIR/bin"
  mkdir -p "$DATA_DIR" "$STUB_DIR"
  cp "$HERE/stub_cloudflared.sh" "$STUB_DIR/cloudflared"
  chmod +x "$STUB_DIR/cloudflared"

  cat > "$DATA_DIR/options.json" <<JSON
{"hostname":"","local_port":18830,"service_token_id":"fake-id","service_token_secret":"fake-secret"}
JSON

  export STUB_LOG="$TMPDIR/stub.log"
  PATH="$STUB_DIR:$PATH" OPTIONS_FILE_OVERRIDE="$DATA_DIR/options.json" sh "$RUN_SH" \
    >"$TMPDIR/stdout.log" 2>"$TMPDIR/stderr.log"
  EXIT_CODE=$?

  assert_eq "$EXIT_CODE" "1" "fehlender hostname -> Exit-Code 1"
  if grep -q "muessen in der Add-on-Konfiguration gesetzt sein" "$TMPDIR/stderr.log"; then
    echo "PASS: Fehlermeldung auf stderr vorhanden"
  else
    echo "FAIL: Fehlermeldung auf stderr fehlt"
    FAIL=1
  fi
  if [ -s "$STUB_LOG" ]; then
    echo "FAIL: cloudflared-Stub wurde trotz fehlender Option aufgerufen"
    FAIL=1
  else
    echo "PASS: cloudflared-Stub wurde korrekt NICHT aufgerufen"
  fi

  rm -rf "$TMPDIR"
}

test_full_options_invokes_cloudflared_correctly
test_missing_hostname_fails_cleanly
exit $FAIL
