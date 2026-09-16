param(
    [switch]$SkipInstaller,
    [switch]$SkipInstallerVerification,
    [string]$IsccPath = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$buildRoot = Join-Path $projectRoot "build\release"
$workRoot = Join-Path $buildRoot "pyinstaller"
$distRoot = Join-Path $projectRoot "dist"
$appDir = Join-Path $distRoot "OfficeFlow"
$installerDir = Join-Path $distRoot "installer"
$icon = Join-Path $buildRoot "officeflow.ico"
$migrationSource = Join-Path $projectRoot "src\officeflow\infrastructure\database\migrations"

if (-not (Test-Path -LiteralPath $python)) {
    throw "가상환경이 없습니다. scripts\bootstrap.ps1을 먼저 실행하세요."
}

function Reset-GeneratedDirectory {
    param([string]$Path)
    $fullPath = [IO.Path]::GetFullPath($Path)
    if (-not $fullPath.StartsWith($projectRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "빌드 경로가 프로젝트 밖입니다: $fullPath"
    }
    if (Test-Path -LiteralPath $fullPath) {
        Remove-Item -LiteralPath $fullPath -Recurse -Force
    }
    New-Item -ItemType Directory -Path $fullPath | Out-Null
}

$version = (& $python -c "from officeflow import __version__; print(__version__)").Trim()
if ($LASTEXITCODE -ne 0 -or -not $version) {
    throw "OfficeFlow 버전을 읽지 못했습니다."
}

Reset-GeneratedDirectory $buildRoot
Reset-GeneratedDirectory $distRoot
New-Item -ItemType Directory -Path $workRoot | Out-Null
New-Item -ItemType Directory -Path $installerDir | Out-Null

& $python (Join-Path $projectRoot "packaging\windows\generate_icon.py") $icon
if ($LASTEXITCODE -ne 0) { throw "아이콘 생성에 실패했습니다." }

$pyinstallerArguments = @(
    "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
    "--name", "OfficeFlow",
    "--paths", (Join-Path $projectRoot "src"),
    "--distpath", $distRoot,
    "--workpath", $workRoot,
    "--specpath", $buildRoot,
    "--icon", $icon,
    "--version-file", (Join-Path $projectRoot "packaging\windows\version_info.txt"),
    "--collect-data", "officeflow",
    "--collect-data", "tzdata",
    "--add-data", "${migrationSource}:officeflow/infrastructure/database/migrations",
    (Join-Path $projectRoot "src\officeflow\main.py")
)
& $python @pyinstallerArguments
if ($LASTEXITCODE -ne 0) { throw "Windows 실행 파일 빌드에 실패했습니다." }

# Qt 6 uses the Windows 10+ system ICU. A developer PATH may contain an unrelated ICU DLL;
# PyInstaller can accidentally collect it and create an incompatible bundle.
Get-ChildItem -LiteralPath (Join-Path $appDir "_internal") -Filter "icu*.dll" -File |
    Remove-Item -Force

# PySide6 6.11 ships a newer compatible VC runtime than the base Python runtime. Keep one
# runtime version at the bundle root so Windows does not load the older copy first.
$pysideRoot = (& $python -c "import PySide6; from pathlib import Path; print(Path(PySide6.__file__).parent)").Trim()
foreach ($runtime in @("VCRUNTIME140.dll", "VCRUNTIME140_1.dll")) {
    Copy-Item -LiteralPath (Join-Path $pysideRoot $runtime) `
        -Destination (Join-Path $appDir "_internal\$runtime") -Force
}

Copy-Item -LiteralPath (Join-Path $projectRoot "packaging\windows\OfficeFlow-사용자안내.html") `
    -Destination (Join-Path $appDir "OfficeFlow-사용자안내.html")

$previousDataDir = $env:OFFICEFLOW_DATA_DIR
$smokeData = Join-Path $buildRoot "smoke-data"
try {
    $env:OFFICEFLOW_DATA_DIR = $smokeData
    $smoke = Start-Process -FilePath (Join-Path $appDir "OfficeFlow.exe") `
        -ArgumentList "--smoke-test" -Wait -PassThru -WindowStyle Hidden
    if ($smoke.ExitCode -ne 0) { throw "패키지 실행 점검에 실패했습니다." }
}
finally {
    $env:OFFICEFLOW_DATA_DIR = $previousDataDir
}

if (-not $SkipInstaller) {
    if (-not $IsccPath) {
        $knownCompilers = @(
            (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
            (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
            (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
        )
        $IsccPath = $knownCompilers | Where-Object { $_ -and (Test-Path -LiteralPath $_) } |
            Select-Object -First 1
    }
    if (-not $IsccPath -or -not (Test-Path -LiteralPath $IsccPath)) {
        throw "Inno Setup 6 컴파일러를 찾지 못했습니다. 설치하거나 -SkipInstaller를 사용하세요."
    }
    & $IsccPath "/DAppVersion=$version" "/DSourceDir=$appDir" `
        "/DOutputDir=$installerDir" "/DSetupIcon=$icon" `
        (Join-Path $projectRoot "packaging\windows\OfficeFlow.iss")
    if ($LASTEXITCODE -ne 0) { throw "설치 프로그램 빌드에 실패했습니다." }

    $installer = Join-Path $installerDir "OfficeFlow-$version-Setup.exe"
    if (-not $SkipInstallerVerification) {
        & (Join-Path $projectRoot "scripts\verify-installer.ps1") -InstallerPath $installer
        if ($LASTEXITCODE -ne 0) { throw "설치 프로그램 검증에 실패했습니다." }
    }
}

$releaseFiles = Get-ChildItem -LiteralPath $distRoot -File -Recurse |
    Where-Object { $_.Name -notin @("SHA256SUMS.txt", "release-manifest.json") }
$checksums = foreach ($file in $releaseFiles) {
    $relative = [IO.Path]::GetRelativePath($distRoot, $file.FullName).Replace("\", "/")
    $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $relative"
}
Set-Content -LiteralPath (Join-Path $distRoot "SHA256SUMS.txt") -Value $checksums -Encoding UTF8

$manifest = [ordered]@{
    product = "OfficeFlow"
    version = $version
    built_at = (Get-Date).ToUniversalTime().ToString("o")
    architecture = $env:PROCESSOR_ARCHITECTURE
    python = (& $python --version)
    files = @($releaseFiles | ForEach-Object {
        [ordered]@{
            path = [IO.Path]::GetRelativePath($distRoot, $_.FullName).Replace("\", "/")
            size = $_.Length
            sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    })
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content `
    -LiteralPath (Join-Path $distRoot "release-manifest.json") -Encoding UTF8

Write-Host "OfficeFlow $version 릴리스 빌드 완료: $distRoot"
