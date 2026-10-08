#!/bin/bash
# Install the native «Interview Cockpit» app + its LaunchAgent, and SUPERSEDE the old Chrome-window
# opener (com.jobfinder.cockpit). Run ON Alan's Mac. Idempotent + reversible (see uninstall.sh).
set -euo pipefail
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
UID_N="$(id -u)"
LA="$HOME/Library/LaunchAgents"
OLD="com.jobfinder.cockpit"
NEW="com.jobfinder.cockpit.native"

# 1) build + install the .app
bash "$SRC_DIR/build.sh"

# 2) supersede the OLD Chrome-window opener (disable, keep for rollback)
launchctl bootout "gui/$UID_N/$OLD" 2>/dev/null || true
if [ -f "$LA/$OLD.plist" ]; then
  mv -f "$LA/$OLD.plist" "$LA/$OLD.plist.disabled"
  echo "[install] disabled old opener ($OLD) -> $OLD.plist.disabled"
fi

# 3) install + (re)load the NEW native LaunchAgent into the GUI session
mkdir -p "$LA"
cp "$SRC_DIR/$NEW.plist" "$LA/$NEW.plist"
launchctl bootout "gui/$UID_N/$NEW" 2>/dev/null || true
launchctl bootstrap "gui/$UID_N" "$LA/$NEW.plist"
launchctl enable "gui/$UID_N/$NEW"
launchctl kickstart -k "gui/$UID_N/$NEW" 2>/dev/null || true

echo "[install] native Interview Cockpit installed + loaded (menu-bar app)."
