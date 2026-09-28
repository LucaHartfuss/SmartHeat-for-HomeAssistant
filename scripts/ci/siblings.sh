#!/usr/bin/env bash
# Baut in CI das Dev-Root-Layout auf: Root-Repo (tools/) + Nachbar-Repos, damit
# tools/contract_check.py und tools/release_gate.py wie lokal laufen (Spec 2.3).
# Kanonisch in HomeAssistant_Dev_Root/tools/ci/siblings.sh; identische Kopien liegen in den
# Unter-Repos unter scripts/ci/siblings.sh (Root-check.sh Schritt "copies" prueft das).
#
# Aufruf: siblings.sh <dev-ziel> <eigener-repo> <eigener-checkout-pfad> <branch> [nachbar-repo...]
# Je Nachbar-Repo wird der gleichnamige Branch genommen, sonst develop. Braucht CROSS_REPO_TOKEN.
set -euo pipefail
DEV="$1"; SELF="$2"; SELF_PATH="$3"; BRANCH="$4"; shift 4
OWNER=LucaHartfuss
RUNBOOK="docs/ci-cd-runbook.md, Abschnitt 'Token erneuern'"
if [ -z "${CROSS_REPO_TOKEN:-}" ]; then
  echo "::error::CROSS_REPO_TOKEN fehlt als Repository-Secret ($RUNBOOK)"; exit 1
fi
status=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $CROSS_REPO_TOKEN" \
  "https://api.github.com/repos/$OWNER/HomeAssistant_Dev_Root")
if [ "$status" != 200 ]; then
  echo "::error::CROSS_REPO_TOKEN ungueltig oder abgelaufen (HTTP $status) ($RUNBOOK)"; exit 1
fi
url() { printf 'https://x-access-token:%s@github.com/%s/%s.git' "$CROSS_REPO_TOKEN" "$OWNER" "$1"; }
ref_for() {
  # Ausserhalb jedes Repos ausfuehren: actions/checkout hinterlegt im Checkout einen extraheader mit dem
  # GITHUB_TOKEN des eigenen Repos, der sonst die Token-URL uebersteuert ("Repository not found").
  if git -C / ls-remote --exit-code --heads "$(url "$1")" "$BRANCH" >/dev/null 2>&1; then echo "$BRANCH"; else echo develop; fi
}
clone() {
  local repo=$1 target=$2 ref
  ref=$(ref_for "$repo")
  echo "$repo -> $ref"
  git clone -q --depth 1 --branch "$ref" "$(url "$repo")" "$target"
}
if [ "$SELF" = HomeAssistant_Dev_Root ]; then
  DEV="$SELF_PATH"
else
  clone HomeAssistant_Dev_Root "$DEV"
  ln -s "$SELF_PATH" "$DEV/$SELF"
fi
for repo in "$@"; do
  [ "$repo" = "$SELF" ] && continue
  clone "$repo" "$DEV/$repo"
done
echo "DEV_ROOT=$DEV" >> "${GITHUB_ENV:-/dev/null}"
echo "Dev-Root: $DEV"
