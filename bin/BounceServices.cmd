@echo off
setlocal EnableExtensions
cd /d C:\OptionsHistory\bin
set "LOG=C:\OptionsHistory\logs\ib_cycle.log"
if not exist "C:\OptionsHistory\logs" mkdir "C:\OptionsHistory\logs"
>>"%LOG%" echo ==== [BounceServices %DATE% %TIME%] ====

"C:\Program Files\nssm-2.24\win64\nssm.exe" stop "IBGateway" >>"%LOG%" 2>&1
timeout /t 5 >nul
powershell -Command "7497,7496 | ForEach-Object { $p=$_; Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { try { Stop-Process -Id $_ -Force } catch {} } }" >>"%LOG%" 2>&1
>>"%LOG%" echo [BounceServices] force-killed port 7497/7496 holders

REM Fix CU: Wait up to 20s for IBGateway NSSM service to reach STOPPED before starting.
REM Without this, rapid restarts get "Unexpected status SERVICE_STOP_PENDING" -> start fails.
for /l %%w in (1,1,20) do (
  "C:\Program Files\nssm-2.24\win64\nssm.exe" status "IBGateway" 2>nul | find /i "SERVICE_STOPPED" >nul && goto gw_stopped
  timeout /t 1 >nul
)
>>"%LOG%" echo [BounceServices] WARN: IBGateway did not reach STOPPED within 20s; starting anyway
:gw_stopped
"C:\Program Files\nssm-2.24\win64\nssm.exe" start "IBGateway" >>"%LOG%" 2>&1
timeout /t 30 >nul

REM Fix CU: Stop OptionsListener and wait up to 20s for it to fully stop before restarting.
REM Without this, sc start gets "FAILED 1056: already running" when stop hasn't completed.
sc stop  OptionsListener   >>"%LOG%" 2>&1
for /l %%w in (1,1,20) do (
  sc query OptionsListener 2>nul | find "STOPPED" >nul && goto ol_stopped
  timeout /t 1 >nul
)
>>"%LOG%" echo [BounceServices] WARN: OptionsListener did not reach STOPPED within 20s; starting anyway
:ol_stopped
sc start OptionsListener   >>"%LOG%" 2>&1

REM Fix DB: Restart CloudflareTunnel AFTER IBGateway+OptionsListener are stable.
REM Was at top with no wait (race condition) — sc start fired while still STOP_PENDING,
REM leaving tunnel broken for the 60s IBGateway restart window.
sc stop CloudflareTunnel >>"%LOG%" 2>&1
for /l %%w in (1,1,15) do (
  sc query CloudflareTunnel 2>nul | find "STOPPED" >nul && goto cf_stopped
  timeout /t 1 >nul
)
>>"%LOG%" echo [BounceServices] WARN: CloudflareTunnel did not reach STOPPED within 15s; starting anyway
:cf_stopped
sc start CloudflareTunnel >>"%LOG%" 2>&1

for /l %%i in (1,1,30) do (
  "%SystemRoot%\System32\curl.exe" -s -o nul --max-time 2 -w "HTTP %%{http_code}" http://127.0.0.1:5001/health | find "HTTP 200" >nul && goto ok
  timeout /t 1 >nul
)
>>"%LOG%" echo WARN: listener /health not ready after bounce
endlocal & exit /b 1
:ok
>>"%LOG%" echo BounceServices complete; listener healthy.
endlocal & exit /b 0
