@echo off
rem Double-click installer bootstrap. The released app carries private Python;
rem this bootstrap therefore uses Windows PowerShell and needs no system Python.
setlocal
cd /d "%~dp0"
title Install Nexus Harness

echo.
echo   Nexus Harness
echo   =============
echo   Downloading the versioned Windows release and verifying its SHA-256.
echo   No administrator account and no separate Python are required.
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install_nexus_harness.ps1"
set "HOW_IT_WENT=%ERRORLEVEL%"

echo.
if "%HOW_IT_WENT%"=="0" (
  echo   Done. Open Nexus Harness from your desktop or Start menu.
) else (
  echo   Installation stopped safely. The exact reason is above.
)
echo.
pause
exit /b %HOW_IT_WENT%
