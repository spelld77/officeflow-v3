param(
    [switch]$RequireOfflineWheels
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$distRoot = Join-Path $projectRoot "dist\python"

if (-not (Test-Path -LiteralPath $python)) {
    throw "가상환경이 없습니다. setup-officeflow-python.cmd를 먼저 실행하세요."
}

$version = (& $python -c "from officeflow import __version__; print(__version__)").Trim()
if ($LASTEXITCODE -ne 0 -or -not $version) {
    throw "OfficeFlow 버전을 읽지 못했습니다."
}

$packageName = "OfficeFlow-Python-$version"
$packageRoot = Join-Path $distRoot $packageName
$archive = Join-Path $distRoot "$packageName.zip"
$resolvedPackage = [IO.Path]::GetFullPath($packageRoot)
$resolvedDist = [IO.Path]::GetFullPath($distRoot) + [IO.Path]::DirectorySeparatorChar
if (-not $resolvedPackage.StartsWith($resolvedDist, [StringComparison]::OrdinalIgnoreCase)) {
    throw "예상하지 못한 Python 배포 경로입니다: $resolvedPackage"
}

if (Test-Path -LiteralPath $packageRoot) {
    Remove-Item -LiteralPath $packageRoot -Recurse -Force
}
if (Test-Path -LiteralPath $archive) {
    Remove-Item -LiteralPath $archive -Force
}

New-Item -ItemType Directory -Path $packageRoot -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $packageRoot "scripts") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $packageRoot "packaging\windows") `
    -Force | Out-Null

foreach ($name in @(
    "pyproject.toml",
    "requirements-runtime.lock",
    "setup-officeflow-python.cmd",
    "run-officeflow-python.cmd",
    "diagnose-officeflow-python.cmd",
    "README.md",
    "CHANGELOG.md"
)) {
    Copy-Item -LiteralPath (Join-Path $projectRoot $name) -Destination $packageRoot
}
Copy-Item -LiteralPath (Join-Path $projectRoot "src") -Destination $packageRoot -Recurse
Copy-Item -LiteralPath (Join-Path $projectRoot "scripts\diagnose_python_runtime.py") `
    -Destination (Join-Path $packageRoot "scripts")
Copy-Item -LiteralPath (Join-Path $projectRoot "docs\11-python-runtime.md") `
    -Destination (Join-Path $packageRoot "Python-실행안내.md")
Copy-Item -LiteralPath `
    (Join-Path $projectRoot "packaging\windows\OfficeFlow-사용자안내.html") `
    -Destination (Join-Path $packageRoot "packaging\windows")

$wheelhouse = Join-Path $projectRoot "wheelhouse"
$wheels = @(
    Get-ChildItem -LiteralPath $wheelhouse -Filter "*.whl" -File -ErrorAction SilentlyContinue
)
if ($wheels.Count) {
    Copy-Item -LiteralPath $wheelhouse -Destination $packageRoot -Recurse
    Write-Host "오프라인 실행 라이브러리 $($wheels.Count)개를 포함합니다."
} elseif ($RequireOfflineWheels) {
    throw "wheelhouse가 없습니다. scripts\download-runtime-wheels.ps1을 먼저 실행하세요."
} else {
    Write-Warning "wheelhouse가 없어 최초 환경 구성 시 인터넷 연결이 필요합니다."
}

Compress-Archive -LiteralPath $packageRoot -DestinationPath $archive -CompressionLevel Optimal
Remove-Item -LiteralPath $packageRoot -Recurse -Force
$hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$archive.sha256.txt" `
    -Value "$hash  $([IO.Path]::GetFileName($archive))" -Encoding UTF8

Write-Host "OfficeFlow Python 배포본 생성 완료: $archive"
