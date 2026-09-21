@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

set "RUNTIME_PYTHON=.venv\Scripts\python.exe"
set "PYTHONUTF8=1"
if not exist "%RUNTIME_PYTHON%" (
    echo [오류] .venv Python 실행 환경이 없습니다.
    echo setup-officeflow-python.cmd를 먼저 실행하세요.
    echo.
    pause
    exit /b 1
)

set "OFFICEFLOW_DATA_DIR="
"%RUNTIME_PYTHON%" scripts\diagnose_python_runtime.py
set "DIAG_EXIT=%ERRORLEVEL%"
echo.
if "%DIAG_EXIT%"=="0" (
    echo 모든 필수 점검을 통과했습니다.
) else (
    echo 실패한 항목이 있습니다. 위 내용을 확인하세요.
)
echo.
pause
exit /b %DIAG_EXIT%
