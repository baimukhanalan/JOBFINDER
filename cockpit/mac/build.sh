#!/bin/bash
# Build + install the native «Interview Cockpit.app» from this dir's Swift source (run ON Alan's Mac).
# Requires only Command Line Tools (swiftc) — NO full Xcode. Idempotent.
set -euo pipefail
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP="/Applications/Interview Cockpit.app"
MACOS="$APP/Contents/MacOS"

echo "[build] compiling InterviewCockpit.swift with swiftc…"
rm -rf "$APP"
mkdir -p "$MACOS"
/usr/bin/swiftc -O \
  -o "$MACOS/InterviewCockpit" \
  "$SRC_DIR/InterviewCockpit.swift" \
  -framework AppKit -framework Foundation
cp "$SRC_DIR/Info.plist" "$APP/Contents/Info.plist"

# ad-hoc sign so Gatekeeper launches a locally-built app; clear any quarantine bit.
/usr/bin/codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
/usr/bin/xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true

echo "[build] built + installed: $APP"
