#!/bin/bash
# Fully remove the Interview Cockpit opener from Alan's Mac. Leaves ~/NativelyInbox data in place
# (delete it by hand if wanted). Does NOT touch Natively, OBS, the camera, or the CDP lane.
set -uo pipefail
PLIST="$HOME/Library/LaunchAgents/com.jobfinder.cockpit.plist"
launchctl bootout "gui/$(id -u)/com.jobfinder.cockpit" 2>/dev/null || true
rm -f "$PLIST"
rm -rf "$HOME/Library/NativelyCockpit"
echo "uninstalled: LaunchAgent + ~/Library/NativelyCockpit removed (NativelyInbox left intact)"
