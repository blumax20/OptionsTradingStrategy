@echo off
setlocal EnableExtensions
cd /d C:\OptionsHistory\bin
set "LOG=C:\OptionsHistory\logs\ib_cycle.log"
if not exist "C:\OptionsHistory\logs" mkdir "C:\OptionsHistory\logs"
>>"%LOG%" echo ==== [BounceServices %DATE% %TIME%] ====

sc stop  CloudflareTunnel  >>"%LOG%" 2>&1
sc start CloudflareTunnel  >>"%LOG%" 2>&1

"C:\Program Files\nssm-2.24\win64\nssm.exe" restart "IBGateway" >>"%LOG%" 2>&1
timeout /t 30 >nul

sc stop  OptionsListener   >>"%LOG%" 2>&1
sc start OptionsListener   >>"%LOG%" 2>&1

for /l %%i in (1,1,30) do (
  "%SystemRoot%\System32\curl.exe" -s -o nul --max-time 2 -w "HTTP %%{http_code}" http://127.0.0.1:5001/health | find "HTTP 200" >nul && goto ok
  timeout /t 1 >nul
)
>>"%LOG%" echo WARN: listener /health not ready after bounce
endlocal & exit /b 1
:ok
>>"%LOG%" echo BounceServices complete; listener healthy.
endlocal & exit /b 0