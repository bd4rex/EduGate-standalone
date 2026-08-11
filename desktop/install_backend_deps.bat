@echo off
setlocal EnableExtensions

title EduGate Standalone - First-time Installation
set "PROJECT_ROOT=%~dp0.."
set "BACKEND_DIR=%PROJECT_ROOT%\backend"
set "DATA_DIR=%PROJECT_ROOT%\data"
set "CONFIG_DIR=%PROJECT_ROOT%\config"
set "VENV_DIR=%PROJECT_ROOT%\runtime\venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "BOOTSTRAP_DIR=%PROJECT_ROOT%\runtime\venv-bootstrap"
set "PYPI_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple"

echo ============================================================
echo EduGate Standalone - First-time Installation
echo ============================================================
echo.
echo Packages will be installed from the Tsinghua PyPI mirror.
echo EduGate data and its private Python environment will be stored in:
echo   %PROJECT_ROOT%
echo.

where py >nul 2>nul
if not errorlevel 1 (
  set "PYTHON=py -3"
) else (
  where python >nul 2>nul
  if not errorlevel 1 (
    set "PYTHON=python"
  ) else (
    echo [ERROR] Python was not found.
    echo Install Python 3.10 or newer and select "Add python.exe to PATH".
    echo Installer: https://www.python.org/downloads/windows/
    pause
    exit /b 1
  )
)

echo [1/6] Checking Python 3.10 or newer...
%PYTHON% --version
%PYTHON% -c "import sys; raise SystemExit(0 if sys.version_info ^>= (3, 10) else 1)"
if %errorlevel% neq 0 (
  echo [ERROR] EduGate requires Python 3.10 or newer.
  pause
  exit /b 1
)

echo.
echo [2/6] Creating the private Python environment...
if not exist "%VENV_PYTHON%" (
  if not exist "%DATA_DIR%" mkdir "%DATA_DIR%"
  %PYTHON% -m venv "%VENV_DIR%"
)
if exist "%VENV_PYTHON%" "%VENV_PYTHON%" -c "import sys" >nul 2>nul
if errorlevel 1 goto :repair_venv
goto :venv_ready

:repair_venv
echo The built-in venv launcher did not work. Using the compatible fallback...
if exist "%VENV_DIR%" rmdir /s /q "%VENV_DIR%"
if not exist "%BOOTSTRAP_DIR%" mkdir "%BOOTSTRAP_DIR%"
%PYTHON% -m pip install -i %PYPI_INDEX% --trusted-host pypi.tuna.tsinghua.edu.cn --upgrade --target "%BOOTSTRAP_DIR%" virtualenv
if errorlevel 1 goto :install_failed
set "PYTHONPATH=%BOOTSTRAP_DIR%"
%PYTHON% -m virtualenv --no-download "%VENV_DIR%"
set "PYTHONPATH="
if not exist "%VENV_PYTHON%" goto :install_failed
for /f "delims=" %%I in ('%PYTHON% -c "import sys; print(sys.executable)"') do set "BASE_PYTHON=%%I"
copy /y "%BASE_PYTHON%" "%VENV_PYTHON%" >nul
"%VENV_PYTHON%" -c "import sys" >nul 2>nul
if errorlevel 1 goto :install_failed

:venv_ready

echo.
echo [3/6] Updating pip from the Tsinghua mirror...
"%VENV_PYTHON%" -m pip install -i %PYPI_INDEX% --trusted-host pypi.tuna.tsinghua.edu.cn --upgrade pip setuptools wheel
if %errorlevel% neq 0 goto :install_failed

echo.
echo [4/6] Installing EduGate dependencies from the Tsinghua mirror...
"%VENV_PYTHON%" -m pip install -i %PYPI_INDEX% --trusted-host pypi.tuna.tsinghua.edu.cn -r "%BACKEND_DIR%\requirements.txt"
if %errorlevel% neq 0 goto :install_failed

echo.
echo [5/6] Creating the local configuration directory...
if not exist "%DATA_DIR%" mkdir "%DATA_DIR%"
if not exist "%CONFIG_DIR%" mkdir "%CONFIG_DIR%"
if not exist "%CONFIG_DIR%\edugate.env" copy "%BACKEND_DIR%\.env.example" "%CONFIG_DIR%\edugate.env" >nul

echo.
echo [6/6] Verifying the installation...
"%VENV_PYTHON%" -c "import fastapi, httpx, uvicorn, pydantic, multipart, pypdf; print('EduGate backend modules are ready.')"
if %errorlevel% neq 0 goto :install_failed

echo.
echo ============================================================
echo [OK] EduGate is ready.
echo ============================================================
echo Next:
echo   1. Close this window.
echo   2. Double-click desktop\run_standalone.bat.
echo   3. Add and test the model-company API in Settings.
echo.
echo The local teacher console signs in automatically.
pause
exit /b 0

:install_failed
echo.
echo ============================================================
echo [FAILED] Installation did not finish.
echo ============================================================
echo The package source used was:
echo   %PYPI_INDEX%
echo.
echo Check that this computer can access pypi.tuna.tsinghua.edu.cn,
echo then run this script again. Existing downloaded packages and
echo EduGate classroom data will not be deleted.
pause
exit /b 1
