:: C:\OptionsHistory\bin\RestartIB.cmd
@echo off
setlocal EnableExtensions
set "LOG=C:\OptionsHistory\logs\ib_cycle.log"
if not exist "C:\OptionsHistory\logs" mkdir "C:\OptionsHistory\logs"
>>"%LOG%" echo ==== [RestartIB %DATE% %TIME%] ====
"C:\Program Files\nssm-2.24\win64\nssm.exe" restart IBGateway >>"%LOG%" 2>&1

set "UP="
for /l %%i in (1,1,20) do (
  netstat -ano | findstr /r /c:":7497 .*LISTENING" /c:":7496 .*LISTENING" >nul && (set "UP=1" & goto _ok)
  timeout /t 1 >nul
)
:_ok
if defined UP (>>"%LOG%" echo IBGateway listening.) else (>>"%LOG%" echo ERROR: IBGateway not listening on 7497/7496)
endlocal & exit /b 0