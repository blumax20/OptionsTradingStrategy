@echo off
setlocal ENABLEDELAYEDEXPANSION

rem ====== Settings ======
set "SVC=OptionsListener"
set "HEALTH_URL=http://127.0.0.1:5001/health"
set "LOGDIR=C:\OptionsHistory\logs"
set "LOG=%LOGDIR%\restart_listener.log"
set "MAX_STOP_WAIT=20"   rem seconds to wait for STOPPED
set "MAX_START_WAIT=25"  rem seconds to wait for RUNNING
set "MAX_HEALTH_WAIT=30" rem seconds to wait for /health 200

if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1

call :ts
>>"%LOG%" echo ==== [RestartListener %%TS%%] ====

rem ----- verify service exists -----
sc query "%SVC%" >nul 2>&1
if errorlevel 1 (
  >>"%LOG%" echo ERROR: service "%SVC%" not found.
  echo Service "%SVC%" not found. See %LOG%
  exit /b 2
)

rem ----- STOP with wait (and force if needed) -----
>>"%LOG%" echo Stopping "%SVC%"...
sc stop "%SVC%" >>"%LOG%" 2>&1

set /a waited=0
:WAIT_STOP
for /f "tokens=3" %%s in ('sc query "%SVC%" ^| findstr /i STATE') do set "STATE=%%s"
if /i "!STATE!"=="STOPPED" goto STOP_OK
if /i "!STATE!"=="RUNNING" (
  if !waited! geq %MAX_STOP_WAIT% goto STOP_FORCE
  timeout /t 1 >nul
  set /a waited+=1
  goto WAIT_STOP
)

rem still transitioning
if !waited! lss %MAX_STOP_WAIT% (
  timeout /t 1 >nul
  set /a waited+=1
  goto WAIT_STOP
)

:STOP_FORCE
>>"%LOG%" echo WARN: stop timed out at !waited!s (state=!STATE!). Attempting taskkill...
rem Try to kill the service process if SCM wrapper lets it hang.
for /f "tokens=2 delims=:" %%P in ('sc queryex "%SVC%" ^| findstr /i "PID"') do (
  set "PID=%%P"
)
set "PID=!PID: =!"
if defined PID (
  >>"%LOG%" echo taskkill /PID !PID! /T /F
  taskkill /PID !PID! /T /F >>"%LOG%" 2>&1
) else (
  >>"%LOG%" echo WARN: could not resolve PID to kill.
)

:STOP_OK
>>"%LOG%" echo Stopped (or not running).

rem ----- START and wait RUNNING -----
>>"%LOG%" echo Starting "%SVC%"...
sc start "%SVC%" >>"%LOG%" 2>&1

set /a waited=0
:WAIT_RUN
for /f "tokens=3" %%s in ('sc query "%SVC%" ^| findstr /i STATE') do set "STATE=%%s"
if /i "!STATE!"=="RUNNING" goto START_OK
if !waited! geq %MAX_START_WAIT% (
  >>"%LOG%" echo ERROR: service failed to enter RUNNING after %MAX_START_WAIT%s (state=!STATE!).
  echo Listener service failed to start; see %LOG%
  exit /b 6
)
timeout /t 1 >nul
set /a waited+=1
goto WAIT_RUN

:START_OK
>>"%LOG%" echo Service is RUNNING; probing health endpoint...

rem ----- HEALTH loop (HTTP 200) -----
set /a waited=0
:WAIT_HEALTH
powershell -NoProfile -Command ^
  "try{ $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 '%HEALTH_URL%'; if($r.StatusCode -eq 200){exit 0}else{exit 1} }catch{ exit 1 }"
if not errorlevel 1 goto HEALTH_OK

if !waited! geq %MAX_HEALTH_WAIT% (
  >>"%LOG%" echo ERROR: listener health did not become 200 within %MAX_HEALTH_WAIT%s.
  echo Listener did not become healthy; see %LOG%
  exit /b 9
)
timeout /t 1 >nul
set /a waited+=1
goto WAIT_HEALTH

:HEALTH_OK
>>"%LOG%" echo OK: listener healthy (HTTP 200).
echo Listener healthy.
exit /b 0

:ts
for /f "tokens=1-3 delims=/ " %%a in ("%date%") do set "D=%%c-%%a-%%b"
for /f "tokens=1-3 delims=:." %%a in ("%time%") do set "T=%%a:%%b:%%c"
set "TS=%D% %T%"
exit /b