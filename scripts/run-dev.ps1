$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "가상환경이 없습니다. scripts\bootstrap.ps1을 먼저 실행하세요."
}

$env:OFFICEFLOW_DATA_DIR = Join-Path $projectRoot ".local-data"
& $python -m officeflow.main
