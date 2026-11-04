#!/bin/bash
# One-time setup, run once per Mac from Terminal:
#   bash ~/Library/Mobile\ Documents/com~apple~CloudDocs/MelaSync/setup.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
source "$HERE/config.sh"
CACHE="${MELASYNC_CACHE:-$HOME/Library/Caches/MelaSync}"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

[[ "$PAGES_PROJECT" == *CHANGEME* ]] && { echo "First edit PAGES_PROJECT in $HERE/config.sh (lowercase letters, digits, dashes)."; exit 1; }

say "Python 3"
if ! "$PYTHON" -c 'import sqlite3, sys; assert sys.version_info >= (3, 9)' 2>/dev/null; then
  echo "Installing Apple's Command Line Tools (provides python3). Re-run this script when it finishes."
  xcode-select --install || true; exit 1
fi
"$PYTHON" --version

say "Node.js"
command -v npm >/dev/null || { echo "Install Node first:  brew install node   (Homebrew: https://brew.sh)"; exit 1; }
node --version

say "Wrangler (Cloudflare CLI), installed privately under $CACHE/tools"
mkdir -p "$CACHE/tools"
npm install --silent --no-fund --no-audit --prefix "$CACHE/tools" wrangler@4 >/dev/null
W="$CACHE/tools/node_modules/.bin/wrangler"; "$W" --version
[ -n "$CLOUDFLARE_ACCOUNT_ID" ] && export CLOUDFLARE_ACCOUNT_ID
TOKEN="$(security find-generic-password -s melasync-cloudflare -w 2>/dev/null || true)"
[ -n "$TOKEN" ] && export CLOUDFLARE_API_TOKEN="$TOKEN"

say "Cloudflare login"
"$W" whoami 2>/dev/null | grep -qi "associated with" || "$W" login

say "Pages project '$PAGES_PROJECT'"
if "$W" pages project list 2>/dev/null | grep -qw "$PAGES_PROJECT"; then
  echo "Exists already (fine on a second Mac) — password unchanged."
else
  "$W" pages project create "$PAGES_PROJECT" --production-branch main
  echo "Choose the site password (you'll type it once on your phone):"
  "$W" pages secret put SITE_PASSWORD --project-name "$PAGES_PROJECT"
fi

say "Reading Mela's database (macOS may ask to allow access to other apps' data — click Allow)"
"$PYTHON" "$HERE/build_site.py" inspect | sed -n '1,3p;/Category join/,/Images table/p'

say "Test sync"
bash "$HERE/sync.sh"

cat <<MSG

Done. Now create the Shortcut (see README.md, "The Shortcut"), or skip it if it
already synced over from your other Mac via iCloud.
Phone: open https://$PAGES_PROJECT.pages.dev, enter the password,
then Chrome menu ⋮ → "Add to Home screen".
MSG
