$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$venvRoot = Join-Path $projectRoot ".venv"
$python = Join-Path $venvRoot "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    python -m venv $venvRoot
}

& $python -m pip install -r (Join-Path $projectRoot "requirements.lock")
& $python -m pip install --no-deps -e $projectRoot

Write-Host "OfficeFlow 개발 환경 준비가 완료됐습니다."
