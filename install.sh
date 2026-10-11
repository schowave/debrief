#!/usr/bin/env bash
# Installs the Debrief as a launchd service for the current user.
#   ./install.sh            install, or reload after changes
#   ./install.sh uninstall  remove the service (files and suggestions are kept)
set -euo pipefail

LABEL="de.$(id -un).debrief"
DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

# Remove the service from before the rename (it was called "transcript-organizer")
OLD_LABEL="de.$(id -un).transcript-organizer"
if [[ -f "$HOME/Library/LaunchAgents/$OLD_LABEL.plist" ]]; then
  launchctl bootout "$DOMAIN/$OLD_LABEL" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$OLD_LABEL.plist"
  echo "Removed old service $OLD_LABEL."
fi

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
if [[ "${1:-}" == "uninstall" ]]; then
  rm -f "$PLIST"
  echo "Removed service $LABEL."
  exit 0
fi

command -v mise >/dev/null || { echo "mise is missing: https://mise.jdx.dev/getting-started.html" >&2; exit 1; }
CLAUDE="$(command -v claude)" || { echo "claude (Claude Code CLI) is missing from PATH" >&2; exit 1; }

cd "$DIR"
# Personal settings are not in Git: create them from the examples on first run
for f in config categories; do
  if [[ ! -f "$f.yaml" ]]; then
    cp "$f.example.yaml" "$f.yaml"
    echo "Created $f.yaml from $f.example.yaml – please adjust it (vault path, name, categories) and run the install again." >&2
    NEEDS_EDIT=1
  fi
done
[[ -z "${NEEDS_EDIT:-}" ]] || exit 1
mise trust -q mise.toml
mise install

# Calendar helper: only rebuild when the code has changed – macOS ties the
# calendar permission to this exact build and would otherwise ask again.
APP="$DIR/bin/Debrief Calendar.app"
HASH="$(shasum calendar/main.swift calendar/build.sh | shasum | cut -c1-16)"
if command -v swiftc >/dev/null; then
  if [[ "$(cat "$DIR/bin/.calendar-build-hash" 2>/dev/null)" != "$HASH" ]]; then
    calendar/build.sh "$DIR/bin" >/dev/null
    echo "$HASH" > "$DIR/bin/.calendar-build-hash"
    echo "Calendar helper built – macOS will now ask for calendar access (please allow)."
  fi
  OUT="$(mktemp)"
  open -W -n --stdout "$OUT" "$APP" --args --auth || true
  grep -q '"ok"' "$OUT" \
    && echo "Calendar access: allowed" \
    || echo "Calendar access: NOT allowed – System Settings → Privacy & Security → Calendars" >&2
  rm -f "$OUT"
else
  echo "swiftc is missing (xcode-select --install) – calendar context stays off." >&2
fi

MISE="$(command -v mise)"
PATH_FOR_SERVICE="$(dirname "$CLAUDE"):$(dirname "$MISE"):/usr/bin:/bin:/usr/sbin:/sbin"
mkdir -p "$(dirname "$PLIST")"
sed -e "s|__LABEL__|$LABEL|g" -e "s|__DIR__|$DIR|g" -e "s|__MISE__|$MISE|g" -e "s|__PATH__|$PATH_FOR_SERVICE|g" \
  launchd.plist.template > "$PLIST"
plutil -lint -s "$PLIST"
launchctl bootstrap "$DOMAIN" "$PLIST"

HOST="$(sed -n 's/^ *host: *\([^ #]*\).*/\1/p' config.yaml | head -1)"
PORT="$(sed -n 's/^ *port: *\([0-9]*\).*/\1/p' config.yaml | head -1)"
echo "Running: http://${HOST:-127.0.0.1}:${PORT:-8765}/  (Log: $DIR/launchd.log)"
