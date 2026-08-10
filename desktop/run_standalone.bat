@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PROJECT_ROOT=%~dp0.."
set "PACKAGED_EXE=%PROJECT_ROOT%\dist\EduGate-Standalone\EduGate-Standalone.exe"
if exist "%PACKAGED_EXE%" (
  start "" "%PACKAGED_EXE%"
  exit /b 0
)

set "VENV_PYTHON=%PROJECT_ROOT%\runtime\venv\Scripts\python.exe"
set "VENV_PYTHONW=%PROJECT_ROOT%\runtime\venv\Scripts\pythonw.exe"
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
