@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

set "RUNTIME_DIR=.venv"
set "RUNTIME_PYTHON=%RUNTIME_DIR%\Scripts\python.exe"
set "PYTHONUTF8=1"

echo [OfficeFlow] Python 실행 환경을 준비합니다.
echo.

if not exist "%RUNTIME_PYTHON%" (
    where py >nul 2>nul
    if errorlevel 1 goto :python_missing

    py -3.12 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>nul
    if errorlevel 1 goto :python_missing

    echo [1/4] Python 3.12 가상환경을 만듭니다.
    py -3.12 -m venv "%RUNTIME_DIR%"
    if errorlevel 1 goto :failed
) else (
    echo [1/4] 기존 가상환경을 사용합니다.
)

"%RUNTIME_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>nul
if errorlevel 1 goto :wrong_runtime

echo [2/4] 실행에 필요한 라이브러리를 확인합니다.
if exist "wheelhouse\*.whl" (
    "%RUNTIME_PYTHON%" -m pip install --disable-pip-version-check --no-index --find-links="wheelhouse" -r requirements-runtime.lock
) else (
    "%RUNTIME_PYTHON%" -m pip install --disable-pip-version-check -r requirements-runtime.lock
)
if errorlevel 1 goto :dependency_failed

echo [3/4] OfficeFlow 소스를 Python 환경에 연결합니다.
"%RUNTIME_PYTHON%" -m pip install --disable-pip-version-check --no-build-isolation --no-deps -e .
if errorlevel 1 goto :failed

echo [4/4] 독립된 임시 데이터로 실행 점검을 합니다.
set "OFFICEFLOW_SMOKE_DIR=%TEMP%\OfficeFlow-Python-Smoke-%RANDOM%-%RANDOM%"
set "OFFICEFLOW_SMOKE_LOG=%TEMP%\OfficeFlow-Python-Smoke-%RANDOM%-%RANDOM%.log"
set "OFFICEFLOW_DATA_DIR=%OFFICEFLOW_SMOKE_DIR%"
set "QT_QPA_PLATFORM=offscreen"
"%RUNTIME_PYTHON%" -m officeflow.main --smoke-test >"%OFFICEFLOW_SMOKE_LOG%" 2>&1
set "SMOKE_EXIT=%ERRORLEVEL%"
set "OFFICEFLOW_DATA_DIR="
set "QT_QPA_PLATFORM="
if exist "%OFFICEFLOW_SMOKE_DIR%" rmdir /s /q "%OFFICEFLOW_SMOKE_DIR%"
if not "%SMOKE_EXIT%"=="0" (
    type "%OFFICEFLOW_SMOKE_LOG%"
    if exist "%OFFICEFLOW_SMOKE_LOG%" del /q "%OFFICEFLOW_SMOKE_LOG%"
    goto :smoke_failed
)
if exist "%OFFICEFLOW_SMOKE_LOG%" del /q "%OFFICEFLOW_SMOKE_LOG%"

echo.
echo [완료] Python 실행 환경을 준비했습니다.
echo 이제 run-officeflow-python.cmd를 더블클릭하면 됩니다.
echo 실제 데이터 위치: %%LOCALAPPDATA%%\OfficeFlow
echo.
pause
exit /b 0

:python_missing
echo [오류] Python 3.12 64비트를 찾지 못했습니다.
echo Python 3.12를 설치한 뒤 다시 실행하세요. Python 3.14는 사용할 수 없습니다.
goto :end_error

:wrong_runtime
echo [오류] 기존 .venv가 Python 3.12로 만들어지지 않았습니다.
echo 기존 .venv의 이름을 바꾸거나 삭제한 뒤 이 파일을 다시 실행하세요.
goto :end_error

:dependency_failed
echo [오류] 실행 라이브러리를 설치하지 못했습니다.
echo 인터넷이 제한된 PC라면 wheelhouse 폴더가 포함된 배포본이 필요합니다.
goto :end_error

:smoke_failed
echo [오류] 실행 점검에 실패했습니다.
echo diagnose-officeflow-python.cmd를 실행해 상태를 확인하세요.
goto :end_error

:failed
echo [오류] Python 실행 환경을 준비하지 못했습니다.

:end_error
echo.
pause
exit /b 1
