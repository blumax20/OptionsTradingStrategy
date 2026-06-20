@echo off
setlocal EnableExtensions
cd /d C:\OptionsHistory\bin

set "LOG=C:\OptionsHistory\logs\ib_cycle.log"
if not exist "C:\OptionsHistory\logs" mkdir "C:\OptionsHistory\logs"
>>"%LOG%" echo ==== [Force_MKT_Close %DATE% %TIME%] ====

REM --- Fix EI: skip if system stopped via PushButton ---
if exist "C:\OptionsHistory\logs\system_stopped.txt" (
  >>"%LOG%" echo [Force_MKT_Close] SKIPPED: system_stopped.txt present -- system stopped via PushButton
  endlocal ^& exit /b 0
)

rem Run DailyCycleManagement to replace limit-close orders with market orders
cd /d "C:\Users\Administrator\code\OptionsTradingStrategy\InteractiveBrokersTrader"

rem optional: breadcrumb/debug
>>"%LOG%" echo [pwd] %CD%
>>"%LOG%" echo [whoami] %USERNAME%

"C:\Users\Administrator\code\OptionsTradingStrategy\.venv\Scripts\python.exe" ^
  ".\DailyCycleManagement.py" --preclose --verbose ^
  >>"%LOG%" 2>&1

set "RC=%ERRORLEVEL%"
>>"%LOG%" echo [Force_MKT_Close] exit=%RC%
endlocal
exit /b %RC%