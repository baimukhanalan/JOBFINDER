#!/bin/bash
# Remove the native «Interview Cockpit» app + LaunchAgent and RESTORE the old Chrome-window opener.
# Run ON Alan's Mac. Does not delete ~/NativelyInbox data.
set -uo pipefail
UID_N="$(id -u)"
LA="$HOME/Library/LaunchAgents"
OLD="com.jobfinder.cockpit"
NEW="com.jobfinder.cockpit.native"

launchctl bootout "gui/$UID_N/$NEW" 2>/dev/null || true
rm -f "$LA/$NEW.plist"
rm -rf "/Applications/Interview Cockpit.app"

# restore the old opener if it was disabled
if [ -f "$LA/$OLD.plist.disabled" ]; then
  mv -f "$LA/$OLD.plist.disabled" "$LA/$OLD.plist"
  launchctl bootstrap "gui/$UID_N" "$LA/$OLD.plist" 2>/dev/null || true
  echo "[uninstall] restored old opener ($OLD)"
fi
echo "[uninstall] native Interview Cockpit removed."
