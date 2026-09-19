@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo OfficeFlow development environment was not found.
    echo Run scripts\bootstrap.ps1 first.
    pause
    exit /b 1
)

set "OFFICEFLOW_DATA_DIR=%CD%\.local-data"
start "OfficeFlow v3" ".venv\Scripts\pythonw.exe" -m officeflow.main
