@echo off
set "LOG=C:\OptionsHistory\logs\ib_cycle.log"
>>"%LOG%" echo ==== [IB_Listener STOP %DATE% %TIME%] ====
sc stop IB_Listener >>"%LOG%" 2>&1
exit /b %ERRORLEVEL%