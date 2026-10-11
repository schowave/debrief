#!/usr/bin/env bash
# Baut „Debrief Kalender.app“ (eigene App, damit macOS den Kalenderzugriff dauerhaft dieser App zuordnet).
#   kalender/build.sh <Zielordner>   (Standard: bin/)
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$SRC/../bin}"
APP="$OUT/Debrief Kalender.app"
BUNDLE_ID="de.$(id -un).debrief.kalender"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
swiftc -O -swift-version 5 "$SRC/main.swift" -o "$APP/Contents/MacOS/kalender"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleName</key><string>Debrief Kalender</string>
  <key>CFBundleExecutable</key><string>kalender</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
  <key>NSCalendarsFullAccessUsageDescription</key>
  <string>Der Debrief liest den Termin zur Aufnahmezeit (Titel, Teilnehmer), um Gespräche zuzuordnen.</string>
  <key>NSCalendarsUsageDescription</key>
  <string>Der Debrief liest den Termin zur Aufnahmezeit (Titel, Teilnehmer), um Gespräche zuzuordnen.</string>
</dict></plist>
PLIST
codesign --force --sign - "$APP"
echo "$APP"
