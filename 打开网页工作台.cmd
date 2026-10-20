@echo off
setlocal
cd /d "%~dp0"
if exist ".venv-hardware\Scripts\python.exe" (
  ".venv-hardware\Scripts\python.exe" ui_server.py --open-browser
) else if exist ".venv-offline\Scripts\python.exe" (
  ".venv-offline\Scripts\python.exe" ui_server.py --open-browser
) else (
  python ui_server.py --open-browser
)
if errorlevel 1 (
  echo.
  echo Could not open ScopeAgent. See README.md for Python and dependency setup.
  pause
)
