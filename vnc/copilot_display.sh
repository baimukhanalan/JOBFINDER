#!/bin/bash
# PM2-managed display stack for Alan's co-pilot (uppercase JOBFINDER). Own display/ports
# so it never collides with the lowercase jobfinder co-pilot (:99 / 5900 / 6080).
# Ensures Xvfb/fluxbox/x11vnc are up, then runs websockify in the FOREGROUND so PM2
# tracks/restarts it. Survives reboot via pm2 save.
export DISPLAY=:98
RES="1280x900x24"

pgrep -f "Xvfb :98" >/dev/null 2>&1 || ( Xvfb :98 -screen 0 "$RES" -ac >/tmp/copilot-alan-xvfb.log 2>&1 & )
sleep 2
pgrep -f "fluxbox -display :98" >/dev/null 2>&1 || ( fluxbox -display :98 >/tmp/copilot-alan-fluxbox.log 2>&1 & )
sleep 1
pgrep -f "x11vnc.*:98" >/dev/null 2>&1 || x11vnc -display :98 -nopw -localhost -forever -shared -rfbport 5901 -bg -o /tmp/copilot-alan-x11vnc.log
sleep 1

exec websockify --web=/usr/share/novnc 127.0.0.1:6090 localhost:5901
