@echo off
rem IBCLogBackup.cmd -- Hourly backup of IBC session log before it gets overwritten
rem Runs via IB_IBCLogBackup_Hourly scheduled task (every hour starting 00:05)
rem Output: C:\OptionsHistory\logs\ibc_backup\IBC-WEEKDAY_YYYYMMDD_HHMM.txt
set "DEST=C:\OptionsHistory\logs\ibc_backup"
if not exist "%DEST%" mkdir "%DEST%"
for %%f in ("C:\IBC\Logs\IBC-*.txt") do (
    copy "%%f" "%DEST%\%%~nf_%DATE:~10,4%%DATE:~4,2%%DATE:~7,2%_%TIME:~0,2%%TIME:~3,2%%%~xf" >nul 2>&1
)
