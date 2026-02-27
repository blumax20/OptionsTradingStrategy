@echo off
setlocal EnableExtensions
cd /d C:\OptionsHistory\bin

set "LOG=C:\OptionsHistory\logs\ib_cycle.log"
if not exist "C:\OptionsHistory\logs" mkdir "C:\OptionsHistory\logs"
>>"%LOG%" echo ==== [SundayReconcile %DATE% %TIME%] ====

rem Reconcile positions vs latest signals (21-day lookback)
rem Places close orders for mismatches so they are ready for Monday market open
cd /d "C:\Users\Administrator\code\OptionsTradingStrategy\InteractiveBrokersTrader"

"C:\Users\Administrator\code\OptionsTradingStrategy\.venv\Scripts\python.exe" ^
  ".\DailyCycleManagement.py" --reconcile --verbose ^
  >>"%LOG%" 2>&1

set "RC=%ERRORLEVEL%"
>>"%LOG%" echo [SundayReconcile] exit=%RC%
endlocal
exit /b %RC%
