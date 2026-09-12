$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "가상환경이 없습니다. scripts\bootstrap.ps1을 먼저 실행하세요."
}

& $python -m ruff check (Join-Path $projectRoot "src") (Join-Path $projectRoot "tests")
& $python -m mypy --config-file (Join-Path $projectRoot "pyproject.toml")
& $python -m pytest --rootdir $projectRoot --cov=officeflow --cov-report=term-missing
