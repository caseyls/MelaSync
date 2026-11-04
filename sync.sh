#!/bin/bash
# One-button Mela -> Cloudflare Pages sync. Run by the "Sync Recipes" Shortcut.
# stdout: a single summary line (shown as the Shortcut notification). Details: last-sync.log
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Shortcuts runs scripts with a bare PATH; add the usual Node locations.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.volta/bin:$HOME/.nvm/current/bin:/usr/bin:/bin:/usr/sbin:/sbin"
source "$HERE/config.sh"
export MELASYNC_TITLE
CACHE="${MELASYNC_CACHE:-$HOME/Library/Caches/MelaSync}"
mkdir -p "$CACHE"
LOG="$CACHE/last-sync.log"
exec 3>&1 4>&2 >"$LOG" 2>&1
echo "== $(date) on $(scutil --get ComputerName 2>/dev/null || hostname)"

fail() { echo "FAILED: $*"; echo "Mela sync failed: $*. Log: $LOG" >&4; exit 1; }

# One sync at a time on this Mac
LOCK="$CACHE/.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +30 2>/dev/null)" ]; then rm -rf "$LOCK"; mkdir "$LOCK"
  else fail "another sync is already running"; fi
fi
trap 'rm -rf "$LOCK"' EXIT

[[ "$PAGES_PROJECT" == *CHANGEME* ]] && fail "edit PAGES_PROJECT in config.sh, then run setup.sh"
"$PYTHON" -c 'import sqlite3' 2>/dev/null || fail "Python 3 missing — run: xcode-select --install"

COUNT="$("$PYTHON" "$HERE/build_site.py" build --out "$CACHE/site" | tail -n1)" || fail "$(tail -n1 "$LOG" | sed 's/^ERROR: //; s/\.$//')"
[[ "$COUNT" =~ ^[0-9]+$ ]] || fail "site build returned no recipe count"

if [[ "${1:-}" != "--no-deploy" ]]; then
  WRANGLER="$CACHE/tools/node_modules/.bin/wrangler"
  [ -x "$WRANGLER" ] || fail "wrangler not installed on this Mac — run setup.sh"
  # Optional API token in the login Keychain (see README); otherwise uses `wrangler login`.
  TOKEN="$(security find-generic-password -s melasync-cloudflare -w 2>/dev/null || true)"
  [ -n "$TOKEN" ] && export CLOUDFLARE_API_TOKEN="$TOKEN"
  [ -n "$CLOUDFLARE_ACCOUNT_ID" ] && export CLOUDFLARE_ACCOUNT_ID
  export WRANGLER_SEND_METRICS=false
  cd "$CACHE/site" || fail "no site folder"
  ok=0
  for wait in 0 5 15; do
    sleep "$wait"
    if "$WRANGLER" pages deploy public --project-name "$PAGES_PROJECT" --branch main \
         --commit-dirty=true --commit-message "MelaSync $(date '+%F %T')"; then ok=1; break; fi
  done
  [ "$ok" = 1 ] || fail "upload to Cloudflare failed"
fi

echo "Synced $COUNT recipes → https://$PAGES_PROJECT.pages.dev" | tee /dev/fd/3
