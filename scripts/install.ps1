param(
    [string]$InstallRoot = "",
    [switch]$NoPath
)

$ErrorActionPreference = "Stop"
$sourceRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "install_helpers.ps1")
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
$pythonArguments = @()
if (-not $pythonCommand) {
    $pythonCommand = Get-Command py -ErrorAction SilentlyContinue
    $pythonArguments = @('-3')
}
if (-not $pythonCommand) {
    throw "Python 3.11 or newer is required on PATH. Install Python, then run this script again."
}
$versionText = & $pythonCommand.Source @pythonArguments -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$parts = $versionText.Split('.')
if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 11)) {
    throw "Python 3.11 or newer is required; found $versionText."
}
if (-not $InstallRoot) {
    $base = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $env:USERPROFILE "AppData\Local" }
    $InstallRoot = Join-Path $base "Programs\OurHarness"
}
$InstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)
$binRoot = Join-Path $InstallRoot "bin"
$appRoot = Join-Path $InstallRoot "app"
$staging = Join-Path $InstallRoot ("stage-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $staging -Force | Out-Null
try {
    $built = Join-Path $staging "harness.pyz"
    & $pythonCommand.Source @pythonArguments (Join-Path $sourceRoot "scripts\build_zipapp.py") --output $built
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $built)) { throw "Zipapp build failed." }
    New-Item -ItemType Directory -Path $binRoot,$appRoot -Force | Out-Null
    $destination = Join-Path $appRoot "harness.pyz"
    Copy-Item -LiteralPath $built -Destination $destination -Force
    $launcher = Join-Path $binRoot "harness.cmd"
    Write-HarnessLauncher -Path $launcher
    & $launcher --version
    if ($LASTEXITCODE -ne 0) { throw "Installed launcher check failed." }
    if (-not $NoPath) {
        $current = [Environment]::GetEnvironmentVariable("Path", "User")
        $segments = @($current -split ';' | Where-Object { $_ })
        if ($segments -notcontains $binRoot) {
            [Environment]::SetEnvironmentVariable("Path", (($segments + $binRoot) -join ';'), "User")
        }
    }
    Write-Output "Installed: $launcher"
    if ($NoPath) { Write-Output "Add this directory to PATH: $binRoot" } else { Write-Output "Open a new terminal, then run: harness init" }
}
finally {
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
}
