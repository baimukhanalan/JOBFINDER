#!/bin/bash
# Virtual display stack for Alan's human-in-the-loop "co-pilot": a real headful Chromium
# the bot pre-fills, viewed/submitted by the human via noVNC in a phone browser.
# Own display/ports (:98 / 5901 / 6081) so it never collides with the lowercase
# jobfinder co-pilot. Everything binds to 127.0.0.1 — exposed ONLY via nginx +
# basic-auth (security rules).
set -e

DISPLAY_NUM=98
RES="1280x900x24"
VNC_PORT=5901
NOVNC_PORT=6090

pkill -f "Xvfb :${DISPLAY_NUM}" 2>/dev/null || true
pkill -f "x11vnc.*:${DISPLAY_NUM}" 2>/dev/null || true
pkill -f "fluxbox -display :${DISPLAY_NUM}" 2>/dev/null || true
pkill -f "websockify.*${NOVNC_PORT}" 2>/dev/null || true
sleep 1

export DISPLAY=":${DISPLAY_NUM}"

# 1) virtual framebuffer
Xvfb ":${DISPLAY_NUM}" -screen 0 "${RES}" -ac >/tmp/copilot-alan-xvfb.log 2>&1 &
sleep 2

# 2) lightweight window manager (so the browser has a frame/maximizes)
fluxbox -display ":${DISPLAY_NUM}" >/tmp/copilot-alan-fluxbox.log 2>&1 &
sleep 1

# 3) VNC server on the display — localhost only
x11vnc -display ":${DISPLAY_NUM}" -nopw -localhost -forever -shared -rfbport "${VNC_PORT}" \
       -bg -o /tmp/copilot-alan-x11vnc.log

# 4) noVNC (websockify) — 127.0.0.1 only, serves the noVNC web client + bridges to VNC
websockify --web=/usr/share/novnc 127.0.0.1:"${NOVNC_PORT}" localhost:"${VNC_PORT}" \
       >/tmp/copilot-alan-websockify.log 2>&1 &
sleep 1

echo "display stack up: DISPLAY=:${DISPLAY_NUM}  vnc=127.0.0.1:${VNC_PORT}  novnc=127.0.0.1:${NOVNC_PORT}"
