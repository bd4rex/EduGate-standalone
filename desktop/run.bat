@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py edugate_desktop.py
) else (
  python edugate_desktop.py
)

