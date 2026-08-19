#!/bin/bash
# PM2-managed display stack for the co-pilot. Ensures Xvfb/fluxbox/x11vnc are up, then
# runs websockify in the FOREGROUND so PM2 tracks/restarts it. Survives reboot via pm2 save.
export DISPLAY=:99
RES="1280x900x24"

pgrep -f "Xvfb :99" >/dev/null 2>&1 || ( Xvfb :99 -screen 0 "$RES" -ac >/tmp/copilot-xvfb.log 2>&1 & )
sleep 2
pgrep -f "fluxbox" >/dev/null 2>&1 || ( fluxbox >/tmp/copilot-fluxbox.log 2>&1 & )
sleep 1
pgrep -f "x11vnc.*:99" >/dev/null 2>&1 || x11vnc -display :99 -nopw -localhost -forever -shared -rfbport 5900 -bg -o /tmp/copilot-x11vnc.log
sleep 1

exec websockify --web=/usr/share/novnc 127.0.0.1:6080 localhost:5900
