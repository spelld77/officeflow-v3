@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

set "RUNTIME_PYTHON=.venv\Scripts\python.exe"
set "RUNTIME_PYTHONW=.venv\Scripts\pythonw.exe"
set "PYTHONUTF8=1"

if not exist "%RUNTIME_PYTHONW%" goto :not_ready

"%RUNTIME_PYTHON%" -c "import officeflow" >nul 2>nul
if errorlevel 1 goto :not_ready

rem Production Python mode always uses the same data as the installed app.
set "OFFICEFLOW_DATA_DIR="
start "" /D "%~dp0" "%RUNTIME_PYTHONW%" -m officeflow.main
exit /b 0

:not_ready
echo OfficeFlow Python 실행 환경이 준비되지 않았습니다.
echo setup-officeflow-python.cmd를 먼저 더블클릭하세요.
echo.
pause
exit /b 1
