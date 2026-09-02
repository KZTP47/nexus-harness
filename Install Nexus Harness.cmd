@echo off
rem Double-click installer bootstrap. The released app carries private Python;
rem this bootstrap therefore uses Windows PowerShell and needs no system Python.
rem Stable offline bundles replace both product-binding placeholders below.
setlocal DisableDelayedExpansion
title Install Nexus Harness
set "NEXUS_BUNDLED_OFFLINE_MODE=__NEXUS_OFFLINE_MODE__"
set "NEXUS_BOOTSTRAP_EXPECTED_SHA256=__NEXUS_OFFLINE_BOOTSTRAP_SHA256__"

echo.
echo   Nexus Harness
echo   =============
echo   Checking this folder for a verified offline Windows installer first.
if "%NEXUS_BUNDLED_OFFLINE_MODE%"=="1" (
  echo   This product-built bundle is offline-only and will never use the network.
) else (
  echo   In this trusted source checkout, an absent bundle uses the stable GitHub release.
)
echo   No administrator account and no separate Python are required.
echo.

set "NEXUS_WINDOWS_POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%NEXUS_WINDOWS_POWERSHELL%" (
  echo   Windows PowerShell was not found at its built-in system path.
  echo   Nexus stopped before running anything from this folder.
  exit /b 1
)

set "NEXUS_BOOTSTRAP_PATH=%~dp0scripts\install_nexus_harness.ps1"
set "NEXUS_BOOTSTRAP_RESOURCE_ROOT=%~dp0scripts"
set "NEXUS_BUNDLE_ROOT=%~dp0."

if "%NEXUS_BUNDLED_OFFLINE_MODE%"=="1" (
  if not exist "%NEXUS_BOOTSTRAP_PATH%" (
    echo   The exact bundled installer bootstrap is missing. Nothing was run.
    exit /b 1
  )
  rem One PowerShell process hashes the open PS1, decodes those same verified
  rem bytes, and executes the resulting in-memory script block. No path or
  rem ancestor directory is resolved again after verification.
  "%NEXUS_WINDOWS_POWERSHELL%" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $p=[Environment]::GetEnvironmentVariable('NEXUS_BOOTSTRAP_PATH'); $r=[Environment]::GetEnvironmentVariable('NEXUS_BOOTSTRAP_RESOURCE_ROOT'); $b=[Environment]::GetEnvironmentVariable('NEXUS_BUNDLE_ROOT'); $e=[Environment]::GetEnvironmentVariable('NEXUS_BOOTSTRAP_EXPECTED_SHA256'); $s=$null; $h=$null; $reader=$null; try{if($e -cnotmatch '\A[0-9a-f]{64}\z'){throw 'The bundled bootstrap pin is malformed.'}; $s=[IO.File]::Open($p,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read); $h=[Security.Cryptography.SHA256]::Create(); $a=-join ($h.ComputeHash($s)|ForEach-Object{$_.ToString('x2')}); if($a -cne $e){throw 'The bundled installer bootstrap failed its product-owned SHA-256 check. Nothing from the bundle was executed.'}; $s.Position=0; $reader=[IO.StreamReader]::new($s,[Text.Encoding]::UTF8,$true,4096,$true); $source=$reader.ReadToEnd(); $reader.Dispose(); $reader=$null; $verified=[scriptblock]::Create($source); & $verified -BundleRoot $b -OfflineOnly -BootstrapResourceRoot $r; if(-not $?){exit 4}}catch{Write-Host ('Installation stopped safely before bootstrap execution: '+$_.Exception.Message) -ForegroundColor Red; exit 3}finally{if($null -ne $reader){$reader.Dispose()};if($null -ne $h){$h.Dispose()};if($null -ne $s){$s.Dispose()}}"
) else (
  rem A source checkout is an explicitly trusted developer/operator surface. It
  rem may use the online fallback and is deliberately not treated as a release ZIP.
  "%NEXUS_WINDOWS_POWERSHELL%" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%NEXUS_BOOTSTRAP_PATH%" -BundleRoot "%~dp0."
)
set "HOW_IT_WENT=%ERRORLEVEL%"

echo.
if "%HOW_IT_WENT%"=="0" (
  echo   Done. Open Nexus Harness from your desktop or Start menu.
) else (
  echo   Installation stopped safely. The exact reason is above.
)
echo.
if /I not "%NEXUS_INSTALLER_NO_PAUSE%"=="1" pause
exit /b %HOW_IT_WENT%
