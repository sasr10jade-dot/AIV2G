@echo off
rem OBD daily batch wrapper (called by Windows Task Scheduler)
rem usage: run_obd_batch.cmd <shadow|live> [live_ev_ids_comma_separated]
setlocal
set "OBD_BATCH_MODE=%~1"
if "%OBD_BATCH_MODE%"=="" set "OBD_BATCH_MODE=shadow"
set "OBD_BATCH_LIVE_EVS=%~2"
set "PYTHONUTF8=1"
cd /d "%~dp0.."
if not exist "data\batch\logs" mkdir "data\batch\logs"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "LOGDATE=%%i"
echo ===== %DATE% %TIME% mode=%OBD_BATCH_MODE% evs=%OBD_BATCH_LIVE_EVS% =====>> "data\batch\logs\batch_%LOGDATE%.log"
python scripts\run_obd_batch.py >> "data\batch\logs\batch_%LOGDATE%.log" 2>&1
set "RC=%ERRORLEVEL%"
echo exit=%RC% >> "data\batch\logs\batch_%LOGDATE%.log"
exit /b %RC%
