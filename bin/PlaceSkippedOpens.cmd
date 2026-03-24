@echo off
setlocal EnableExtensions
cd /d C:\OptionsHistory\bin

set "LOG=C:\OptionsHistory\logs\ib_cycle.log"
if not exist "C:\OptionsHistory\logs" mkdir "C:\OptionsHistory\logs"
>>"%LOG%" echo ==== [PlaceSkippedOpens %DATE% %TIME%] ====

REM Retry OPEN orders skipped last evening (no_viable_limit_or_conditions) at 10:00 AM
REM after the 9:45 AM CSV enrichment has populated live prices (Fix CP)
>>"%LOG%" echo [PlaceSkippedOpens] calling DailyCycleManagement.py --place-skipped-opens ...
cd /d "C:\Users\Administrator\code\OptionsTradingStrategy\InteractiveBrokersTrader"
set "PY=C:\Users\Administrator\code\OptionsTradingStrategy\.venv\Scripts\python.exe"
"%PY%" ".\DailyCycleManagement.py" --place-skipped-opens >>"%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
>>"%LOG%" echo [PlaceSkippedOpens] exit=%RC%
endlocal & exit /b %RC%
