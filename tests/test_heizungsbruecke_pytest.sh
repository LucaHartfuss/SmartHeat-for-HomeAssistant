#!/bin/bash
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ADDON_DIR="$HERE/../heizungsbruecke"

echo "--- heizungsbruecke: pytest ---"
# PYTEST_DISABLE_PLUGIN_AUTOLOAD verhindert, dass pytest global installierte Plugins aus
# anderen, voellig unabhaengigen Repos dieses Multi-Repo-Projekts automatisch laedt (z.B.
# pytest-homeassistant-custom-component, das unter Windows mit
# "ModuleNotFoundError: No module named 'fcntl'" crasht) -- diese Suite braucht keine
# Autoload-Plugins.
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
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
