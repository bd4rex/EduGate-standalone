@echo off
setlocal EnableExtensions
cd /d "%~dp0.."

set "BUILD_VENV=%CD%\.venv-build"
set "BUILD_PYTHON=%BUILD_VENV%\Scripts\python.exe"
set "BOOTSTRAP_DIR=%CD%\.venv-bootstrap"
set "PYPI_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple"

where py >nul 2>nul
if %errorlevel% neq 0 (
  echo [ERROR] Python launcher "py" was not found.
  exit /b 1
)

py -3 -c "import operator,sys; raise SystemExit(0 if operator.ge(sys.version_info, (3, 10)) else 1)"
if %errorlevel% neq 0 (
  echo [ERROR] EduGate builds require Python 3.10 or newer.
  exit /b 1
)

if not exist "%BUILD_PYTHON%" py -3 -m venv "%BUILD_VENV%"
if exist "%BUILD_PYTHON%" "%BUILD_PYTHON%" -c "import sys" >nul 2>nul
if errorlevel 1 goto :repair_venv
goto :venv_ready

:repair_venv
echo The built-in venv launcher did not work. Using the compatible fallback...
if exist "%BUILD_VENV%" rmdir /s /q "%BUILD_VENV%"
if not exist "%BOOTSTRAP_DIR%" mkdir "%BOOTSTRAP_DIR%"
py -3 -m pip install -i %PYPI_INDEX% --trusted-host pypi.tuna.tsinghua.edu.cn --upgrade --target "%BOOTSTRAP_DIR%" virtualenv
if errorlevel 1 exit /b 1
set "PYTHONPATH=%BOOTSTRAP_DIR%"
py -3 -m virtualenv --no-download "%BUILD_VENV%"
set "PYTHONPATH="
if not exist "%BUILD_PYTHON%" exit /b 1
for /f "delims=" %%I in ('py -3 -c "import sys; print(sys.executable)"') do set "BASE_PYTHON=%%I"
copy /y "%BASE_PYTHON%" "%BUILD_PYTHON%" >nul
"%BUILD_PYTHON%" -c "import sys" >nul 2>nul
if errorlevel 1 exit /b 1

:venv_ready

"%BUILD_PYTHON%" -m pip install -i %PYPI_INDEX% --trusted-host pypi.tuna.tsinghua.edu.cn --upgrade pip pyinstaller
if %errorlevel% neq 0 exit /b 1

"%BUILD_PYTHON%" -m pip install -i %PYPI_INDEX% --trusted-host pypi.tuna.tsinghua.edu.cn -r backend\requirements.txt
if %errorlevel% neq 0 exit /b 1

"%BUILD_PYTHON%" -m PyInstaller --noconfirm --clean desktop\edugate_standalone.spec
if %errorlevel% neq 0 exit /b 1

echo.
echo Adding the portable classroom Python runtime...
"%BUILD_PYTHON%" desktop\build_portable_runtime.py dist\EduGate-Standalone\runtime\python
if %errorlevel% neq 0 exit /b 1

copy /y desktop\PORTABLE-README.txt dist\EduGate-Standalone\README.txt >nul

echo.
echo [OK] Windows bundle created at:
echo   %CD%\dist\EduGate-Standalone
