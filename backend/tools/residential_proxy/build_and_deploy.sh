#!/bin/bash
# Reproducible build + deploy of the residential-proxy delivery layer.
#   - cross-compiles the Go client (client/) for Windows/macOS/Linux, baking the chisel tunnel
#     credential in via -ldflags (read from dist/chisel_auth — gitignored, never committed)
#   - generates the landing page + mac/win wrappers into dist/ (all gitignored)
#   - deploys them to the PUBLIC web dir /var/www/proxy-plain/ (served at https://proxy.systeam.kz/)
# One-time server setup (NOT here — see README): the jf-chisel systemd service (chisel server bound
# 127.0.0.1:8096, --authfile restricting the credential to R:127.0.0.1:8120..8129), the
# proxy.systeam.kz nginx vhost (public page at /, chisel WSS at /link) + certbot cert.
# Re-run this after changing the client or rotating the credential. Requires: go on PATH,
# dist/chisel_auth present (the "user:pass" that matches /opt/jf-chisel/auth on the server).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
D="$HERE/dist"
WEB="/var/www/proxy-plain"
AUTH="$(cat "$D/chisel_auth")"   # gitignored — kept out of the repo
export PATH="/usr/local/go/bin:$PATH" GOFLAGS="-mod=mod" GOTOOLCHAIN=auto

echo "== cross-compile (auth baked via ldflags, kept out of git) =="
cd "$HERE/client"
LD="-s -w -X main.chiselAuth=$AUTH"
CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build -ldflags "-H=windowsgui $LD" -o "$D/win.exe" .
CGO_ENABLED=0 GOOS=darwin  GOARCH=amd64 go build -ldflags "$LD" -o "$D/mac-amd64" .
CGO_ENABLED=0 GOOS=darwin  GOARCH=arm64 go build -ldflags "$LD" -o "$D/mac-arm64" .
CGO_ENABLED=0 GOOS=linux   GOARCH=amd64 go build -ldflags "$LD" -o "$D/linux-amd64" .

echo "== generate web assets =="
# mac.command: fetch the right-arch binary from the PUBLIC path, install autostart, run
cat > "$D/mac.command" <<'EOF'
#!/bin/bash
set -e
BASE="$HOME/.jobfinder-proxy"; mkdir -p "$BASE"
arch=amd64; [ "$(uname -m)" = "arm64" ] && arch=arm64
echo "JobFinder residential proxy — устанавливаю…"
curl -fsSL "https://proxy.systeam.kz/mac-$arch" -o "$BASE/agent"
chmod +x "$BASE/agent"; xattr -dr com.apple.quarantine "$BASE/agent" 2>/dev/null || true
"$BASE/agent"
echo "Готово ✓  Работает в фоне, автозапуск при входе. Это окно можно закрыть."
EOF
cat > "$D/mac-off.command" <<'EOF'
#!/bin/bash
"$HOME/.jobfinder-proxy/agent" --uninstall 2>/dev/null || true
echo "Отключено ✓  Это окно можно закрыть."
EOF
printf '@echo off\r\n"%%LOCALAPPDATA%%\\jobfinder-proxy\\agent.exe" --uninstall\r\necho Otklyucheno. Mozhno zakryt okno.\r\npause\r\n' > "$D/win-off.bat"
chmod +x "$D/mac.command" "$D/mac-off.command"
cp "$HERE/index.html" "$D/index.html"

echo "== deploy to $WEB (public) =="
sudo mkdir -p "$WEB"
sudo cp "$D"/{index.html,win.exe,mac.command,mac-off.command,win-off.bat,mac-amd64,mac-arm64,linux-amd64} "$WEB/"
sudo chmod -R 755 "$WEB"
echo "deployed. PUBLIC URL: https://proxy.systeam.kz/"
