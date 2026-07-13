@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "VENV_PYTHON=%LOCALAPPDATA%\EduGate\venv\Scripts\python.exe"
set "VENV_PYTHONW=%LOCALAPPDATA%\EduGate\venv\Scripts\pythonw.exe"
if not exist "%VENV_PYTHON%" (
  echo EduGate has not been installed on this computer yet.
  echo The first-time installer will now open and use the Tsinghua PyPI mirror.
  echo.
  call "%~dp0install_backend_deps.bat"
  if errorlevel 1 exit /b 1
)

if not exist "%VENV_PYTHONW%" set "VENV_PYTHONW=%VENV_PYTHON%"
start "" /b "%VENV_PYTHONW%" "%~dp0edugate_standalone.py"
exit /b 0
