#!/bin/bash
# Start virtual display stack for auto-apply browser viewing
# Access via: http://173.249.18.153:6080/vnc.html

set -e

DISPLAY_NUM=99
RESOLUTION="1920x1080x24"
VNC_PORT=5900
NOVNC_PORT=6080

echo "Starting virtual display stack..."

# Kill existing processes
pkill -f "Xvfb :${DISPLAY_NUM}" 2>/dev/null || true
pkill -f "x11vnc.*:${DISPLAY_NUM}" 2>/dev/null || true
pkill -f "fluxbox.*:${DISPLAY_NUM}" 2>/dev/null || true
pkill -f "websockify.*${NOVNC_PORT}" 2>/dev/null || true
sleep 1

# 1. Start Xvfb (virtual display)
Xvfb :${DISPLAY_NUM} -screen 0 ${RESOLUTION} -ac &
sleep 1
echo "Xvfb started on :${DISPLAY_NUM}"

# 2. Start fluxbox window manager
DISPLAY=:${DISPLAY_NUM} fluxbox &
sleep 1
echo "Fluxbox started"

# 3. Start x11vnc (VNC server)
x11vnc -display :${DISPLAY_NUM} -forever -nopw -rfbport ${VNC_PORT} -shared -quiet &
sleep 1
echo "x11vnc started on port ${VNC_PORT}"

# 4. Start websockify + noVNC
NOVNC_DIR=$(find /usr -name "vnc.html" -path "*/novnc/*" 2>/dev/null | head -1 | xargs dirname 2>/dev/null || echo "/usr/share/novnc")
websockify --web="${NOVNC_DIR}" ${NOVNC_PORT} localhost:${VNC_PORT} &
sleep 1
echo "noVNC started on port ${NOVNC_PORT}"

echo ""
echo "========================================="
echo "Browser viewable at: http://173.249.18.153:${NOVNC_PORT}/vnc.html"
echo "========================================="
echo ""
echo "Display stack running. Press Ctrl+C to stop."

# Keep script running
wait
