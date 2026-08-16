function Write-HarnessLauncher {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    $launcherDirectory = Split-Path -Parent $Path
    $powershellLauncher = Join-Path $launcherDirectory "harness-launcher.ps1"
    $powershellBody = @'
$ErrorActionPreference = 'Stop'
$application = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\app\harness.pyz'))
if (-not (Test-Path -LiteralPath $application -PathType Leaf)) {
    throw "Harness application was not found beside the launcher: $application"
}
$python = Get-Command python -ErrorAction SilentlyContinue
$pythonArguments = @()
if (-not $python) {
    $python = Get-Command py -ErrorAction SilentlyContinue
    $pythonArguments = @('-3')
}
if (-not $python) {
    throw 'Python 3.11 or newer is required on PATH.'
}
& $python.Source @pythonArguments $application @args
exit $LASTEXITCODE
'@
    $utf8WithBom = [System.Text.UTF8Encoding]::new($true)
    [System.IO.File]::WriteAllText($powershellLauncher, $powershellBody, $utf8WithBom)

    $launcherBody = "@echo off`r`npowershell.exe -NoProfile -ExecutionPolicy Bypass -File `"%~dp0harness-launcher.ps1`" %*`r`nexit /b %ERRORLEVEL%`r`n"
    [System.IO.File]::WriteAllText($Path, $launcherBody, [System.Text.Encoding]::ASCII)
}
