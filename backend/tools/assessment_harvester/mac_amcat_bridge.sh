#!/usr/bin/env bash
# Server-side bridge to drive the owner's MacBook Chrome (REAL webcam + live face) over the Tailscale
# tunnel, so a Sutherland/AMCAT assessment passes the WCI200 camera-device wall AND the proctoring
# liveness/gaze check that flags a synthetic session. The harvester's answer-bank / module handlers run
# here on the server; only the browser + camera live on the Mac.
#
# TRANSPORT: socat listens on 127.0.0.1:$LOCAL_PORT and pipes each connection through `tailscale nc`
# (via the userspace tailscaled joined to the phones' tailnet) to the MacBook's tailnet IP:$MAC_PORT,
# where a MacBook-side socat forwards to Chrome's loopback 127.0.0.1:9222. Playwright connects to
# http://127.0.0.1:$LOCAL_PORT so Chrome sees a 127.0.0.1 Host header (its DNS-rebind guard accepts it).
#
# PREREQ on the MacBook (owner runs, see the chat instructions):
#   1) Chrome:  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
#                 --remote-debugging-port=9222 --use-fake-ui-for-media-stream --user-data-dir=/tmp/amcat
#   2) forward: socat TCP-LISTEN:9223,bind=$MAC_IP,reuseaddr,fork TCP:127.0.0.1:9222
#
# USAGE:
#   mac_amcat_bridge.sh --check                      # just bring up the tunnel + confirm CDP reachable
#   mac_amcat_bridge.sh --url <invite> --mailbox <m> # + drive the assessment via the harvester
set -u
MAC_IP="${MAC_IP:-100.86.135.112}"
MAC_PORT="${MAC_PORT:-9223}"
LOCAL_PORT="${LOCAL_PORT:-9222}"
SOCK="${TS_SOCK:-/home/projects/jobfinder/backend/data/ts-egress/0/tailscaled.sock}"
REPO="/home/projects/jobfinder"
URL=""; MAILBOX="silas.bailey8826@takhet.com"; CHECK_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --url) URL="$2"; shift 2;;
    --mailbox) MAILBOX="$2"; shift 2;;
    --check) CHECK_ONLY=1; shift;;
    *) echo "unknown arg: $1"; exit 2;;
  esac
done

# fresh tunnel
pkill -f "TCP-LISTEN:${LOCAL_PORT}.*nc ${MAC_IP}" 2>/dev/null
sleep 0.5
echo "[bridge] socat 127.0.0.1:${LOCAL_PORT} -> tailscale nc ${MAC_IP} ${MAC_PORT}"
socat TCP-LISTEN:${LOCAL_PORT},bind=127.0.0.1,reuseaddr,fork \
  EXEC:"tailscale --socket=${SOCK} nc ${MAC_IP} ${MAC_PORT}" &
SOCAT_PID=$!
trap 'kill $SOCAT_PID 2>/dev/null' EXIT
sleep 1.5

# confirm the remote Chrome DevTools endpoint answers through the tunnel
echo "[bridge] probing CDP endpoint ..."
VER=$(timeout 15 curl -s "http://127.0.0.1:${LOCAL_PORT}/json/version" 2>/dev/null)
if echo "$VER" | grep -qi "webSocketDebuggerUrl\|Chrome/"; then
  echo "[bridge] CDP OK: $(echo "$VER" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("Browser"),"|",d.get("webSocketDebuggerUrl","")[:60])' 2>/dev/null)"
else
  echo "[bridge] CDP NOT reachable. Is the MacBook running Chrome (--remote-debugging-port=9222) + the socat forward on ${MAC_IP}:${MAC_PORT}?"
  echo "[bridge] raw: ${VER:0:200}"
  exit 1
fi

if [ "$CHECK_ONLY" = "1" ] || [ -z "$URL" ]; then
  echo "[bridge] tunnel up. HARVEST_CDP_URL=http://127.0.0.1:${LOCAL_PORT}"
  echo "[bridge] (check-only) leaving tunnel up for 60s; re-run with --url to drive."
  sleep 60; exit 0
fi

echo "[bridge] driving assessment on the MacBook Chrome: $URL"
cd "$REPO" || exit 1
HARVEST_CDP_URL="http://127.0.0.1:${LOCAL_PORT}" HARVEST_SESSION_SECS="${HARVEST_SESSION_SECS:-5100}" \
  python3 -m backend.tools.harvest_runner --platform shl_sutherland --url "$URL" --mailbox "$MAILBOX"
