#!/bin/bash
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ADDON_DIR="$HERE/../heizungsbruecke"

echo "--- heizungsbruecke: pytest ---"
if command -v pytest >/dev/null 2>&1; then
  (cd "$ADDON_DIR" && pytest)
else
  (cd "$ADDON_DIR" && python -m pytest)
fi
EXIT_CODE=$?

if [ "$EXIT_CODE" = "0" ]; then
  echo "PASS: heizungsbruecke pytest-Suite erfolgreich"
else
  echo "FAIL: heizungsbruecke pytest-Suite fehlgeschlagen (Exit-Code $EXIT_CODE)"
fi

exit $EXIT_CODE
