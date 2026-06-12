@echo off
setlocal
cd /d "%~dp0..\backend"
where py >nul 2>nul
if %errorlevel%==0 (
  set PYTHON=py
) else (
  set PYTHON=python
)

%PYTHON% -m pip install --user -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn -r requirements.txt
if %errorlevel% neq 0 (
  echo.
  echo Tsinghua mirror install failed. Trying local proxy http://127.0.0.1:7890...
  set HTTP_PROXY=http://127.0.0.1:7890
  set HTTPS_PROXY=http://127.0.0.1:7890
  set ALL_PROXY=http://127.0.0.1:7890
  %PYTHON% -m pip install --user -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn -r requirements.txt
)
pause
