@echo off
setlocal EnableExtensions
set "LOG=C:\OptionsHistory\logs\snapshot_1458.log"
set "BACKUP=C:\OptionsHistory\logs\ibc_backup"

if not exist "C:\OptionsHistory\logs" mkdir "C:\OptionsHistory\logs"
if not exist "%BACKUP%" mkdir "%BACKUP%"

>>"%LOG%" echo ==== [Snapshot %DATE% %TIME%] ====

rem Back up current IBC session log before any 3PM event can overwrite it
for %%f in ("C:\IBC\Logs\IBC-*.txt") do (
    copy "%%f" "%BACKUP%\%%~nf_%DATE:~10,4%%DATE:~4,2%%DATE:~7,2%_%TIME:~0,2%%TIME:~3,2%%%~xf" >nul 2>&1
    >>"%LOG%" echo Backed up IBC log: %%~nxf
)

rem IBGateway process PID -- a PID change between runs = restart occurred
>>"%LOG%" echo --- IBGateway process (PID):
tasklist 2>nul | findstr /i "ibgateway" >>"%LOG%"

rem All processes currently connected to port 7496 (API clients)
>>"%LOG%" echo --- Port 7496 connections:
netstat -ano 2>nul | findstr ":7496 " >>"%LOG%"

rem Service states
>>"%LOG%" echo --- Service states:
sc query IBGateway 2>nul | findstr "STATE" >>"%LOG%"
sc query OptionsListener 2>nul | findstr "STATE" >>"%LOG%"
sc query CloudflareTunnel 2>nul | findstr "STATE" >>"%LOG%"

>>"%LOG%" echo ----
endlocal
