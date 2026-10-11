#!/usr/bin/env bash
# Installiert den Debrief als launchd-Dienst des aktuellen Benutzers.
#   ./install.sh            installieren bzw. nach Änderungen neu laden
#   ./install.sh uninstall  Dienst entfernen (Dateien und Vorschläge bleiben)
set -euo pipefail

LABEL="de.$(id -un).debrief"
DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

# Dienst aus der Zeit vor der Umbenennung (hieß „transcript-organizer“) entfernen
OLD_LABEL="de.$(id -un).transcript-organizer"
if [[ -f "$HOME/Library/LaunchAgents/$OLD_LABEL.plist" ]]; then
  launchctl bootout "$DOMAIN/$OLD_LABEL" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$OLD_LABEL.plist"
  echo "Alten Dienst $OLD_LABEL entfernt."
fi

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
if [[ "${1:-}" == "uninstall" ]]; then
  rm -f "$PLIST"
  echo "Dienst $LABEL entfernt."
  exit 0
fi

command -v mise >/dev/null || { echo "mise fehlt: https://mise.jdx.dev/getting-started.html" >&2; exit 1; }
CLAUDE="$(command -v claude)" || { echo "claude (Claude Code CLI) fehlt im PATH" >&2; exit 1; }

cd "$DIR"
# Persönliche Einstellungen liegen nicht im Git: beim ersten Mal aus den Beispielen anlegen
for f in config kategorien; do
  if [[ ! -f "$f.yaml" ]]; then
    cp "$f.example.yaml" "$f.yaml"
    echo "$f.yaml aus $f.example.yaml angelegt – bitte anpassen (Vault-Pfad, Name, Kategorien) und erneut installieren." >&2
    NEEDS_EDIT=1
  fi
done
[[ -z "${NEEDS_EDIT:-}" ]] || exit 1
mise trust -q mise.toml
mise install

# Kalender-Hilfsprogramm: nur neu bauen, wenn sich der Code geändert hat – macOS bindet die
# Kalender-Erlaubnis an genau diesen Build und würde sonst erneut fragen.
APP="$DIR/bin/Debrief Kalender.app"
HASH="$(shasum kalender/main.swift kalender/build.sh | shasum | cut -c1-16)"
if command -v swiftc >/dev/null; then
  if [[ "$(cat "$DIR/bin/.kalender-build-hash" 2>/dev/null)" != "$HASH" ]]; then
    kalender/build.sh "$DIR/bin" >/dev/null
    echo "$HASH" > "$DIR/bin/.kalender-build-hash"
    echo "Kalender-Hilfsprogramm gebaut – macOS fragt gleich nach dem Kalenderzugriff (bitte erlauben)."
  fi
  OUT="$(mktemp)"
  open -W -n --stdout "$OUT" "$APP" --args --auth || true
  grep -q '"ok"' "$OUT" \
    && echo "Kalenderzugriff: erlaubt" \
    || echo "Kalenderzugriff: NICHT erlaubt – Systemeinstellungen → Datenschutz & Sicherheit → Kalender" >&2
  rm -f "$OUT"
else
  echo "swiftc fehlt (xcode-select --install) – Kalender-Kontext bleibt aus." >&2
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
echo "Läuft: http://${HOST:-127.0.0.1}:${PORT:-8765}/  (Log: $DIR/launchd.log)"
