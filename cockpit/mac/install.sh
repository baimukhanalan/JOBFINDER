#!/bin/bash
# Install the Interview Cockpit opener on Alan's Mac (reversible — see uninstall.sh).
# Run FROM the Mac (the server scp's this dir to ~/NativelyCockpit.staging first), or via:
#   ssh macalan 'bash ~/NativelyCockpit.staging/install.sh'
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/Library/NativelyCockpit"
AGENTS="$HOME/Library/LaunchAgents"
PLIST="$AGENTS/com.jobfinder.cockpit.plist"

mkdir -p "$DEST" "$AGENTS" "$HOME/NativelyInbox"
cp "$HERE/cockpit_open.py" "$DEST/cockpit_open.py"
chmod +x "$DEST/cockpit_open.py"
sed "s#__HOME__#$HOME#g" "$HERE/com.jobfinder.cockpit.plist" > "$PLIST"

# reload cleanly (modern launchd)
launchctl bootout "gui/$(id -u)/com.jobfinder.cockpit" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/com.jobfinder.cockpit" 2>/dev/null || true
echo "installed: $DEST/cockpit_open.py + $PLIST (polls every 5 min + at login/wake)"
echo "manual open now:  /usr/bin/python3 $DEST/cockpit_open.py --now"
