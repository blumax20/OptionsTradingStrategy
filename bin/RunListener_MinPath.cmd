@echo off
setlocal
set "VENV=C:\Users\Administrator\code\OptionsTradingStrategy\.venv\Scripts"
set "APPDIR=C:\Users\Administrator\code\OptionsTradingStrategy\InteractiveBrokersTrader"
set "SYS=C:\Windows\System32;C:\Windows"
set "PATH=%VENV%;%SYS%"
set "PYLAUNCHER_NO_SEARCH=1"
set "PYTHONEXECUTABLE=%VENV%\python.exe"

if not exist "C:\OptionsHistory\logs" mkdir "C:\OptionsHistory\logs" >nul 2>&1
echo %DATE% %TIME% USER=%USERNAME% >> C:\OptionsHistory\logs\listener_launch.log
"%VENV%\python.exe" -c "import os,sys,getpass; print('PYEXE='+sys.executable); print('USER='+getpass.getuser())" 1>>C:\OptionsHistory\logs\listener_launch.log 2>&1

cd /d "%APPDIR%"
"%VENV%\python.exe" "C:\OptionsHistory\bin\boot_listener.py"
