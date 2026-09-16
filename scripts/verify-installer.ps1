param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$resolvedInstaller = (Resolve-Path -LiteralPath $InstallerPath).Path
$testRoot = Join-Path $projectRoot "build\installer-verification"
$installDir = Join-Path $testRoot "installed"
$dataDir = Join-Path $testRoot "user-data"

function Invoke-CheckedProcess {
    param([string]$FilePath, [string[]]$Arguments)
    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -ne 0) {
        throw "프로세스가 실패했습니다($($process.ExitCode)): $FilePath"
    }
}

$fullTestRoot = [IO.Path]::GetFullPath($testRoot)
if (-not $fullTestRoot.StartsWith($projectRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "설치 검증 경로가 프로젝트 밖입니다: $fullTestRoot"
}
if (Test-Path -LiteralPath $fullTestRoot) {
    Remove-Item -LiteralPath $fullTestRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $fullTestRoot | Out-Null

$previousDataDir = $env:OFFICEFLOW_DATA_DIR
$previousPath = $env:PATH
try {
    $installArguments = @(
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER", "/NOICONS",
        "/DIR=$installDir"
    )
    Invoke-CheckedProcess $resolvedInstaller $installArguments
    $executable = Join-Path $installDir "OfficeFlow.exe"
    if (-not (Test-Path -LiteralPath $executable)) {
        throw "설치 후 OfficeFlow.exe를 찾지 못했습니다."
    }

    $env:OFFICEFLOW_DATA_DIR = $dataDir
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    Invoke-CheckedProcess $executable @("--smoke-test")
    $env:PATH = $previousPath
    $database = Join-Path $dataDir "data\officeflow.db"
    if (-not (Test-Path -LiteralPath $database)) {
        throw "깨끗한 설치 실행 후 데이터베이스가 생성되지 않았습니다."
    }
    $sentinel = Join-Path $dataDir "preserve-through-upgrade.txt"
    Set-Content -LiteralPath $sentinel -Value "preserve" -Encoding UTF8

    Invoke-CheckedProcess $resolvedInstaller $installArguments
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    Invoke-CheckedProcess $executable @("--smoke-test")
    $env:PATH = $previousPath
    if (-not (Test-Path -LiteralPath $sentinel)) {
        throw "업그레이드 재설치 중 사용자 데이터가 손실되었습니다."
    }

    $uninstaller = Get-ChildItem -LiteralPath $installDir -Filter "unins*.exe" |
        Select-Object -First 1
    if ($null -eq $uninstaller) {
        throw "제거 프로그램을 찾지 못했습니다."
    }
    Invoke-CheckedProcess $uninstaller.FullName @(
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"
    )
    if (Test-Path -LiteralPath $executable) {
        throw "제거 후 실행 파일이 남아 있습니다."
    }
    if (-not (Test-Path -LiteralPath $sentinel)) {
        throw "제거 과정에서 사용자 데이터가 삭제되었습니다."
    }
    Write-Host "설치, 업그레이드, 제거 및 사용자 데이터 보존 검증 통과"
}
finally {
    $env:OFFICEFLOW_DATA_DIR = $previousDataDir
    $env:PATH = $previousPath
    $remainingUninstaller = Get-ChildItem -LiteralPath $installDir -Filter "unins*.exe" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $remainingUninstaller) {
        Start-Process -FilePath $remainingUninstaller.FullName -ArgumentList @(
            "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"
        ) -Wait -WindowStyle Hidden | Out-Null
    }
    if (Test-Path -LiteralPath $fullTestRoot) {
        Remove-Item -LiteralPath $fullTestRoot -Recurse -Force
    }
}
