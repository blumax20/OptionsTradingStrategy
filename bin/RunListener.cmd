# Create the folder if needed
New-Item -Type Directory -Force C:\OptionsHistory\bin | Out-Null

# Write the wrapper (CRLF line endings on Windows)
@'
@echo off
setlocal
set "VENV=C:\Users\Administrator\code\OptionsTradingStrategy\.venv\Scripts"
set "APPDIR=C:\Users\Administrator\code\OptionsTradingStrategy\InteractiveBrokersTrader"

REM Prevent py.exe from searching another interpreter
set PYLAUNCHER_NO_SEARCH=1
REM Tell any re-exec which interpreter to use
set PYTHONEXECUTABLE=%VENV%\python.exe
REM Put venv first in PATH
set PATH=%VENV%;%PATH%

cd /d "%APPDIR%"
"%VENV%\python.exe" "%APPDIR%\listener.py" --serve
'@ | Set-Content -Path C:\OptionsHistory\bin\RunListener.cmd -Encoding ASCII