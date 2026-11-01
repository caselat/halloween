@echo off
title Connect Dropbox
cd /d "%~dp0"
if not exist ".venv32\Scripts\python.exe" (
  echo Run start.bat once first, then run this again.
  pause
  exit /b 1
)
".venv32\Scripts\python.exe" dropbox_setup.py
