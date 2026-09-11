@echo off
rem ============================================================
rem  Script Builder - one-click setup (Windows)
rem
rem  What it does:
rem    1. checks Python + Node.js
rem    2. creates .venv and installs the engine dependencies
rem    3. installs the desktop app dependencies (Electron)
rem    4. verifies that the engine can start
rem
rem  Why ASCII-only output on purpose: a .cmd is parsed using the current
rem  console code page, so non-ASCII text in here is a portability minefield.
rem  The Chinese guide lives in README.md.
rem ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo === Script Builder setup ===
echo Project: %CD%
echo.

rem ---------- 1. Python ----------
where py >nul 2>nul
if errorlevel 1 (
  where python >nul 2>nul
  if errorlevel 1 (
    echo [x] Python not found. Install Python 3.11+ from https://www.python.org/downloads/
    echo     Remember to tick "Add python.exe to PATH" during installation.
    pause
    exit /b 1
  )
  set "PYBASE=python"
) else (
  set "PYBASE=py -3"
)
echo [1/4] Python found.
%PYBASE% --version

rem ---------- 2. engine venv ----------
echo.
echo [2/4] Creating .venv and installing engine dependencies...
if not exist ".venv\Scripts\python.exe" (
  %PYBASE% -m venv .venv
  if errorlevel 1 (
    echo [x] Failed to create the virtual environment.
    pause
    exit /b 1
  )
)
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [x] Failed to install engine dependencies. Check your network / proxy.
  pause
  exit /b 1
)

rem ---------- 3. app dependencies ----------
echo.
echo [3/4] Installing the desktop app dependencies (Electron)...
where npm >nul 2>nul
if errorlevel 1 (
  echo [x] Node.js / npm not found. Install Node.js 18+ from https://nodejs.org/
  pause
  exit /b 1
)
pushd app
if not exist "node_modules\electron\dist\electron.exe" (
  call npm install
  if errorlevel 1 (
    echo [x] npm install failed.
    popd
    pause
    exit /b 1
  )
) else (
  echo     already installed, skipping.
)
popd

rem ---------- 4. verify ----------
echo.
echo [4/4] Verifying the engine...
".venv\Scripts\python.exe" -c "import mss, numpy, cv2, PIL, win32gui, pynput; print('    engine deps OK')"
if errorlevel 1 (
  echo [x] Some engine dependencies are missing.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import sys; sys.path.insert(0, '.'); import engine.schema; print('    engine imports OK')"
if errorlevel 1 (
  echo [x] The engine package could not be imported.
  pause
  exit /b 1
)

echo.
echo === Done ===
echo Start the app with:  start-app.cmd      (or: cd app ^&^& npm start)
echo.
echo Optional: cloud AI (Zhipu glm-4.6v) for semantic verdict / code translation.
echo   Put your key in  .secrets\zhipu.env  as a single line:  ZHIPU_API_KEY=your_key
echo   It is git-ignored and never uploaded. Everything else runs fully offline.
echo.
pause
endlocal
