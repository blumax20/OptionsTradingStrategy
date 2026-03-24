@echo off
setlocal EnableExtensions
set "LOG=C:\OptionsHistory\logs\ib_cycle.log"
set "PY=C:\Users\Administrator\code\OptionsTradingStrategy\.venv\Scripts\python.exe"

if not exist "C:\OptionsHistory\logs" mkdir "C:\OptionsHistory\logs"

>>"%LOG%" echo ==== [PrewarmConnections %DATE% %TIME%] ====

cd /d "C:\OptionsHistory\bin"
"%PY%" "PrewarmApiConnections.py" >>"%LOG%" 2>&1

set "RC=%ERRORLEVEL%"
>>"%LOG%" echo [PrewarmConnections] exit=%RC%
endlocal
exit /b %RC%
