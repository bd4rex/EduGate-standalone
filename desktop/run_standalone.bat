@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "VENV_PYTHON=%LOCALAPPDATA%\EduGate\venv\Scripts\python.exe"
if not exist "%VENV_PYTHON%" (
  echo EduGate has not been installed on this computer yet.
  echo The first-time installer will now open and use the Tsinghua PyPI mirror.
  echo.
  call "%~dp0install_backend_deps.bat"
  if errorlevel 1 exit /b 1
)

"%VENV_PYTHON%" "%~dp0edugate_standalone.py"
if %errorlevel% neq 0 (
  echo.
  echo EduGate stopped because of an error. See the message above.
  pause
)
