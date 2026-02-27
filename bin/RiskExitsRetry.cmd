@echo off
setlocal EnableExtensions
cd /d C:\OptionsHistory\bin

set "LOG=C:\OptionsHistory\logs\ib_cycle.log"
if not exist "C:\OptionsHistory\logs" mkdir "C:\OptionsHistory\logs"
>>"%LOG%" echo ==== [RiskExitsRetry %DATE% %TIME%] ====

REM Run 10:30 AM risk exits retry (second attempt after 9:35 AM open)
>>"%LOG%" echo [RiskExitsRetry] calling DailyCycleManagement.py --risk-exits-only ...
cd /d "C:\Users\Administrator\code\OptionsTradingStrategy\InteractiveBrokersTrader"
set "PY=C:\Users\Administrator\code\OptionsTradingStrategy\.venv\Scripts\python.exe"
"%PY%" ".\DailyCycleManagement.py" --risk-exits-only >>"%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
>>"%LOG%" echo [RiskExitsRetry] exit=%RC%
endlocal & exit /b %RC%
