@echo off
rem Internal Wi-Fi dev server (server PC 192.168.35.92, port 3000)
rem usage: scripts\run_dev_lan.cmd [port]
rem   - binds 0.0.0.0 so other PCs/phones on the same Wi-Fi can open http://192.168.35.92:3000/
rem   - /dev quick-login works for anyone on the LAN: never expose this port to the internet
rem   - first run only (admin PowerShell) to open the firewall:
rem       New-NetFirewallRule -DisplayName "AIV2G dev 3000" -Direction Inbound -Protocol TCP -LocalPort 3000 -Profile Private -Action Allow
setlocal
set "PORT=%~1"
if "%PORT%"=="" set "PORT=3000"
set "PYTHONUTF8=1"
if "%ROOT_REDIRECT%"=="" set "ROOT_REDIRECT=/dev"
cd /d "%~dp0.."
echo AIV2G dev server: http://192.168.35.92:%PORT%/  (local: http://127.0.0.1:%PORT%/dev)
python -m uvicorn src.api:app --host 0.0.0.0 --port %PORT% --reload
