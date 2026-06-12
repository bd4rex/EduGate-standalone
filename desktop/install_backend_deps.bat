@echo off
setlocal EnableExtensions

title EduGate Standalone - Install Backend Dependencies
cd /d "%~dp0..\backend"

echo ============================================================
echo EduGate Standalone dependency installer
echo ============================================================
echo.
echo This script installs the Python packages required by the
echo local EduGate backend on this teacher computer.
echo.
echo Project folder:
echo %CD%
echo.

where py >nul 2>nul
if %errorlevel%==0 (
  set PYTHON=py
) else (
  where python >nul 2>nul
  if %errorlevel%==0 (
    set PYTHON=python
  ) else (
    echo [ERROR] Python was not found.
    echo.
    echo Please install Python 3.9 or newer, then run this script again.
    echo Recommended Windows installer: https://www.python.org/downloads/windows/
    echo During installation, tick "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
  )
)

echo [1/5] Checking Python...
%PYTHON% --version
if %errorlevel% neq 0 (
  echo [ERROR] Python exists but cannot run normally.
  pause
  exit /b 1
)

echo.
echo [2/5] Checking pip...
%PYTHON% -m pip --version
if %errorlevel% neq 0 (
  echo [ERROR] pip is not available for this Python.
  echo Try: %PYTHON% -m ensurepip --upgrade
  pause
  exit /b 1
)

echo.
echo [3/5] Upgrading pip bootstrap tools if available...
%PYTHON% -m pip install --user -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn --upgrade pip setuptools wheel

echo.
echo [4/5] Installing backend dependencies from Tsinghua PyPI mirror...
%PYTHON% -m pip install --user -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn -r requirements.txt
if %errorlevel% neq 0 (
  echo.
  echo [WARN] Tsinghua mirror install failed.
  echo Trying again through local proxy http://127.0.0.1:7890 ...
  echo This helps on computers where Windows proxy points to Clash/V2Ray.
  set HTTP_PROXY=http://127.0.0.1:7890
  set HTTPS_PROXY=http://127.0.0.1:7890
  set ALL_PROXY=http://127.0.0.1:7890
  %PYTHON% -m pip install --user -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn -r requirements.txt
)

if %errorlevel% neq 0 (
  echo.
  echo ============================================================
  echo [FAILED] Dependency installation did not finish.
  echo ============================================================
  echo.
  echo Please check:
  echo 1. The computer can access the Internet.
  echo 2. If using a proxy, make sure the proxy app is running.
  echo 3. If the proxy is 127.0.0.1:7890, it should accept HTTP proxy traffic.
  echo 4. Try running this command manually:
  echo.
  echo    %PYTHON% -m pip install --user -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn -r requirements.txt
  echo.
  pause
  exit /b 1
)

echo.
echo [5/5] Verifying installed modules...
%PYTHON% -c "import fastapi, httpx, uvicorn, pydantic, multipart, pypdf; print('All backend modules are installed.')"
if %errorlevel% neq 0 (
  echo [ERROR] Packages were installed, but verification failed.
  pause
  exit /b 1
)

if not exist ".env" (
  echo.
  echo Creating backend\.env from backend\.env.example ...
  copy ".env.example" ".env" >nul
)

echo.
echo ============================================================
echo [OK] EduGate backend dependencies are ready.
echo ============================================================
echo.
echo Next steps:
echo 1. Run desktop\run_standalone.bat
echo 2. Open the teacher console from the launcher
echo 3. Login with: admin / edugate
echo 4. Before real classroom use, edit backend\.env:
echo    - ADMIN_PASSWORD
echo    - ADMIN_API_KEY
echo    - UPSTREAM_API_KEY
echo.
pause
