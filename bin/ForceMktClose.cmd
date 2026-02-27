@echo off
setlocal EnableExtensions
cd /d C:\OptionsHistory\bin

set "LOG=C:\OptionsHistory\logs\ib_cycle.log"
if not exist "C:\OptionsHistory\logs" mkdir "C:\OptionsHistory\logs"
>>"%LOG%" echo ==== [Force_MKT_Close %DATE% %TIME%] ====

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