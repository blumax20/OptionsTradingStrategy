@echo off
setlocal EnableExtensions
cd /d C:\OptionsHistory\bin

set "LOG=C:\OptionsHistory\logs\ib_cycle.log"
if not exist "C:\OptionsHistory\logs" mkdir "C:\OptionsHistory\logs"
>>"%LOG%" echo ==== [Place_Open %DATE% %TIME%] ====

REM --- Fix EI: skip if system stopped via PushButton ---
if exist "C:\OptionsHistory\logs\system_stopped.txt" (
  >>"%LOG%" echo [PlaceOpen] SKIPPED: system_stopped.txt present -- system stopped via PushButton
  endlocal ^& exit /b 0
)

REM --- Step 0: run DCM first (time-aware: at 17:00 it enforces closes + from-signal) ---
>>"%LOG%" echo [PlaceOpen] calling DailyCycleManagement.py (time-aware after-hours pipeline)...
cd /d "C:\Users\Administrator\code\OptionsTradingStrategy\InteractiveBrokersTrader"
set "PY=C:\Users\Administrator\code\OptionsTradingStrategy\.venv\Scripts\python.exe"
"%PY%" ".\DailyCycleManagement.py" >>"%LOG%" 2>&1
set "DCMRC=%ERRORLEVEL%"
>>"%LOG%" echo [PlaceOpen] DCM exit=%DCMRC%

REM --- Compute today's CSV path using NY date ---
set "CSV="
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command ^
  "$tz='Eastern Standard Time';" ^
  "$d=[System.TimeZoneInfo]::ConvertTime([datetime]::UtcNow,[System.TimeZoneInfo]::FindSystemTimeZoneById($tz));" ^
  "$p='C:\OptionsHistory\{0}\combined_listener_spreads.csv' -f $d.ToString('yy_MM_dd');" ^
  "Write-Output $p"`) do set "CSV=%%P"
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "Split-Path -Parent '%CSV%'"`) do set "CSVDIR=%%D"
>>"%LOG%" echo [PlaceOpen] expected CSV: "%CSV%"
>>"%LOG%" echo [PlaceOpen] expected CSVDIR: "%CSVDIR%"

REM --- Optional fallback: run from-signal again like before (if CSV exists) ---
if %DCMRC% NEQ 0 if exist "%CSV%" (
  >>"%LOG%" echo CSV present; DCM failed; fallback placing from-signal...
  "%PY%" ".\PlaceAnOrder.py" --mode from-signal --quantity 1 --min-limit 0.05 >>"%LOG%" 2>&1
  >>"%LOG%" echo Place_Open fallback completed. RC=%ERRORLEVEL%
  endlocal & exit /b %ERRORLEVEL%
)

>>"%LOG%" echo NOTE: CSV missing after DCM; no fallback from-signal placement.
endlocal & exit /b %DCMRC%