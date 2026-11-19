@echo off
rem Internal Wi-Fi dev server (server PC 192.168.35.92, port 5050)
rem usage: scripts\run_dev_lan.cmd [port]
rem   - binds 0.0.0.0 so other PCs/phones on the same Wi-Fi can open http://192.168.35.92:5050/
rem   - /dev quick-login is limited to LAN (private/loopback IP, no proxy headers) by QUICK_LOGIN_LAN_ONLY=1
rem     still: never expose this port to the internet (port forwarding / tunnel)
rem   - first run only (admin PowerShell) to open the firewall:
rem       New-NetFirewallRule -DisplayName "AIV2G dev 5050" -Direction Inbound -Protocol TCP -LocalPort 5050 -Profile Private -Action Allow
setlocal
set "PORT=%~1"
if "%PORT%"=="" set "PORT=5050"
set "PYTHONUTF8=1"
set "QUICK_LOGIN_LAN_ONLY=1"
if "%ROOT_REDIRECT%"=="" set "ROOT_REDIRECT=/dev"
cd /d "%~dp0.."
echo AIV2G dev server: http://192.168.35.92:%PORT%/  (local: http://127.0.0.1:%PORT%/dev)
python -m uvicorn src.api:app --host 0.0.0.0 --port %PORT% --reload
