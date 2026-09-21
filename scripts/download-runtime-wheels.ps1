param(
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$wheelhouse = if ($OutputDirectory) {
    [IO.Path]::GetFullPath($OutputDirectory)
} else {
    Join-Path $projectRoot "wheelhouse"
}

if (-not (Test-Path -LiteralPath $python)) {
    throw "가상환경이 없습니다. setup-officeflow-python.cmd를 먼저 실행하세요."
}

$version = (& $python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
if ($version -ne "3.12") {
    throw "Python 3.12 환경에서만 오프라인 패키지를 만들 수 있습니다. 현재: $version"
}

New-Item -ItemType Directory -Path $wheelhouse -Force | Out-Null
& $python -m pip download --disable-pip-version-check --only-binary=:all: `
    --dest $wheelhouse -r (Join-Path $projectRoot "requirements-runtime.lock")
if ($LASTEXITCODE -ne 0) {
    throw "실행 라이브러리 다운로드에 실패했습니다."
}

Write-Host "OfficeFlow 오프라인 wheelhouse 준비 완료: $wheelhouse"
