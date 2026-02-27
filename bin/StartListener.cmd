@echo off
setlocal EnableExtensions

rem Always log to the cycle file
set "LOG=C:\OptionsHistory\logs\ib_cycle.log"
if not exist "C:\OptionsHistory\logs" mkdir "C:\OptionsHistory\logs"
>>"%LOG%" echo ==== [OptionsListener START %DATE% %TIME%] ====

rem Fast-path: if service RUNNING and :5001 is LISTENING, bail out
sc query OptionsListener | find "RUNNING" >nul
if %ERRORLEVEL% EQU 0 (
  netstat -ano | findstr /i ":5001" | findstr /i "LISTENING" >nul
  if %ERRORLEVEL% EQU 0 (
    >>"%LOG%" echo OptionsListener already RUNNING and 5001 listening; skipping start.
    endlocal & exit /b 0
  )
)

rem If PAUSED, try continue before recycling
sc query OptionsListener | find "PAUSED" >nul
if %ERRORLEVEL% EQU 0 (
  sc continue OptionsListener >>"%LOG%" 2>&1
  for /l %%i in (1,1,20) do (
    sc query OptionsListener | find "RUNNING" >nul && goto _svc_running_or_stop
    ping -n 2 127.0.0.1 >nul
  )
)
:_svc_running_or_stop

rem Kill any system-Python that might be holding 5001
powershell -NoProfile -Command ^
  "$cons = Get-NetTCPConnection -State Listen 2>$null | ? { $_.LocalPort -eq 5001 };" ^
  "foreach($c in $cons){" ^
  "  $p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $c.OwningProcess);" ^
  "  if($p -and $p.ExecutablePath -match '\\\\Program Files\\\\Python312\\\\python.exe'){" ^
  "    Stop-Process -Id $p.ProcessId -Force" ^
  "  }" ^
  >>"%LOG%" 2>&1

rem Ensure service is STOPPED (avoid STOP_PENDING)
sc query OptionsListener | find "STOPPED" >nul
if not %ERRORLEVEL% EQU 0 (
  sc stop OptionsListener >>"%LOG%" 2>&1
  for /l %%i in (1,1,60) do (
    sc query OptionsListener | find "STOPPED" >nul && goto _svc_stopped
    ping -n 2 127.0.0.1 >nul
  )
)
:_svc_stopped

rem Bounce Cloudflare & IBGateway in a controlled way
>>"%LOG%" echo Restarting "CloudflareTunnel" service...
sc stop  CloudflareTunnel >>"%LOG%" 2>&1
for /l %%i in (1,1,40) do (
  sc query CloudflareTunnel | find "STOPPED" >nul && goto _cf_stopped
  ping -n 2 127.0.0.1 >nul
)
:_cf_stopped
sc start CloudflareTunnel >>"%LOG%" 2>&1

>>"%LOG%" echo Restarting "IBGateway" via NSSM...
"C:\Program Files\nssm-2.24\win64\nssm.exe" restart "IBGateway" >>"%LOG%" 2>&1
timeout /t 10 /nobreak >>"%LOG%" 2>&1

rem Start the listener service
>>"%LOG%" echo sc start OptionsListener
sc start OptionsListener >>"%LOG%" 2>&1

rem Wait until service RUNNING
for /l %%i in (1,1,40) do (
  sc query OptionsListener | find "RUNNING" >nul && goto _svc_ready
  ping -n 2 127.0.0.1 >nul
)
:_svc_ready

rem Wait for :5001 LISTENING and make sure owner is NOT system Python
set "TRIES=0"
:wait5001
set "PID="
for /f "tokens=5" %%c in ('netstat -ano ^| findstr /i ":5001" ^| findstr /i "LISTENING"') do set "PID=%%c"
if not defined PID (
  set /A TRIES+=1
  if %TRIES% GEQ 60 goto porttimeout
  ping -n 1 127.0.0.1 >nul
  goto wait5001
)

rem We have a PID; check the executable path for system Python
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command ^
  "$p=Get-CimInstance Win32_Process -Filter 'ProcessId=%PID%'; if($p){$p.ExecutablePath}"`) do (
  echo %%P | findstr /i "\\Program Files\\Python312\\python.exe" >nul
  if %ERRORLEVEL% EQU 0 (
    powershell -NoProfile -Command "Stop-Process -Id %PID% -Force" >>"%LOG%" 2>&1
    set "PID="
    goto wait5001
  )
)

>>"%LOG%" echo Port 5001 is listening with PID %PID%.

rem Final health probe
where curl >nul 2>&1
if %ERRORLEVEL% EQU 0 (
  for /f "tokens=* delims=" %%H in ('curl -s -o nul -w "HTTP %%{http_code}" http://127.0.0.1:5001/health') do (
    >>"%LOG%" echo /health: %%H
  )
) else (
  powershell -NoProfile -Command ^
    "try{(Invoke-WebRequest -UseBasicParsing -TimeoutSec 6 http://127.0.0.1:5001/health).StatusCode}catch{$_|Out-String}" >>"%LOG%" 2>&1
)

>>"%LOG%" echo DONE.
endlocal & exit /b 0

:porttimeout
>>"%LOG%" echo ERROR: Port 5001 did not open in time.
endlocal & exit /b 1