@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo Python is not on PATH. Install Python 3.11+ and try again.
  pause
  exit /b 1
)

python -c "import numpy, Crypto" >nul 2>&1
if errorlevel 1 (
  echo Installing Python dependencies ^(one-time^)...
  python -m pip install -r "daemon/requirements.txt"
  if errorlevel 1 (
    echo pip install failed.
    pause
    exit /b 1
  )
)

cd /d "%~dp0app"
if not exist "node_modules/electron" (
  echo Installing Electron ^(one-time, ~200MB^)...
  call npm install --no-fund --no-audit
  if errorlevel 1 (
    echo npm install failed.
    pause
    exit /b 1
  )
)

rem Electron spawns daemon\main.py itself and talks JSON-RPC over stdio.
rem There is no HTTP server and no port to wait on.
call npx --yes electron . %*
endlocal
