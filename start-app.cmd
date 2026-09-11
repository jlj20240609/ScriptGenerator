@echo off
rem ============================================================
rem  Start "Script Builder" - double click this file.
rem  Equivalent to:  cd app  &&  npm start
rem
rem  ASCII-only on purpose: a .cmd is parsed using the current console
rem  code page, so non-ASCII text here breaks on some machines.
rem  Chinese guide: README.md
rem ============================================================
setlocal
cd /d "%~dp0app"

if not exist "node_modules\.bin\electron.cmd" (
  echo [x] Dependencies are not installed yet.
  echo     Run setup.cmd in the project root first ^(or: npm install^).
  echo.
  pause
  exit /b 1
)

echo Starting Script Builder... ^(close the window to exit^)
call "node_modules\.bin\electron.cmd" .
if errorlevel 1 (
  echo.
  echo [x] Failed to start. Exit code %errorlevel%.
  echo     Make sure the engine virtual env exists: run setup.cmd first.
  pause
)
endlocal
