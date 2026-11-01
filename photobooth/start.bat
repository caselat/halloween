@echo off
title Photobooth
cd /d "%~dp0"

rem Canon's camera software is 32-bit, so the booth runs on 32-bit Python 3.11.
where py >nul 2>nul || goto nopython
py -3.11-32 -c "pass" >nul 2>nul && goto havepy
echo Installing 32-bit Python 3.11 (one time)...
winget install -e --id Python.Python.3.11 --architecture x86 --accept-package-agreements --accept-source-agreements
py -3.11-32 -c "pass" >nul 2>nul || goto nopython

:havepy
if not exist ".venv32\Scripts\python.exe" (
  echo First run: setting up, this takes a minute...
  py -3.11-32 -m venv .venv32 || goto fail
)
".venv32\Scripts\python.exe" -m pip install -q --disable-pip-version-check --only-binary=:all: -r requirements.txt || goto fail
".venv32\Scripts\python.exe" launch.py
pause
exit /b 0

:nopython
echo.
echo Couldn't set up 32-bit Python automatically. Open PowerShell and run:
echo     winget install -e --id Python.Python.3.11 --architecture x86
echo then double-click start.bat again.
pause
exit /b 1

:fail
echo.
echo Setup failed. Send Claude the messages above.
pause
exit /b 1
