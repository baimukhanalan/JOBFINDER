@echo off
REM JobFinder residential proxy — double-click me. Keeps a reverse tunnel to the server up so it
REM can egress through this laptop's home internet. Leave the window open.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-proxy-windows.ps1"
echo.
echo (the tunnel window closed - press a key to exit)
pause >nul
