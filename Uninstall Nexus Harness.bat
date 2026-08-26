@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Uninstall Nexus Harness

rem ===========================================================================
rem  Remove Nexus Harness from this Windows account.
rem
rem  This file is intentionally self-contained. It does not depend on Python,
rem  the repository folder, a version number, or a particular user name.
rem
rem  Options:
rem    /S         do not prompt or pause (also passed to the NSIS uninstaller)
rem    /DRY-RUN   show what would be removed without changing anything
rem
rem  Project folders, settings, transcripts, evidence, and provider sign-ins
rem  are user data. They are deliberately preserved.
rem ===========================================================================

set "NEXUS_QUIET=0"
set "NEXUS_DRY_RUN=0"
set "NEXUS_FAILED=0"
set "NEXUS_FOUND=0"

:read_arguments
if "%~1"=="" goto arguments_done
if /I "%~1"=="/S" set "NEXUS_QUIET=1"
if /I "%~1"=="/SILENT" set "NEXUS_QUIET=1"
if /I "%~1"=="/DRY-RUN" set "NEXUS_DRY_RUN=1"
shift
goto read_arguments

:arguments_done
echo.
echo   Nexus Harness uninstaller
echo   =========================
echo.
echo   This removes the app, command-line launcher, Start menu entry, and
echo   Nexus Harness desktop shortcut from your Windows account.
echo.
echo   Your projects, settings, transcripts, evidence, and web-provider
echo   sign-ins will be kept.
echo.

if "%NEXUS_DRY_RUN%"=="1" echo   DRY RUN: nothing will be changed.& echo.
if "%NEXUS_QUIET%"=="1" goto confirmed

choice /C YN /N /M "  Continue? [Y/N] "
if errorlevel 2 (
  echo.
  echo   Nothing was changed.
  echo.
  exit /b 0
)

:confirmed
rem The Electron builder has always used a version-free per-user directory.
rem Keep the product-name spelling as a second candidate so a future packaging
rem rename, or an older preview build, can still be removed by this same file.
if defined LOCALAPPDATA (
  call :run_uninstallers "%LOCALAPPDATA%\Programs\our-harness-desktop"
  if errorlevel 1 goto uninstall_failed
  call :run_uninstallers "%LOCALAPPDATA%\Programs\Nexus Harness"
  if errorlevel 1 goto uninstall_failed
)

rem Remove the shortcut made by Install Nexus Harness.cmd. The usual desktop,
rem OneDrive desktops, and Windows' actual Known Folder are all considered.
if defined USERPROFILE call :remove_shortcut "%USERPROFILE%\Desktop\Nexus Harness.lnk"
if defined OneDrive call :remove_shortcut "%OneDrive%\Desktop\Nexus Harness.lnk"
if defined OneDriveCommercial call :remove_shortcut "%OneDriveCommercial%\Desktop\Nexus Harness.lnk"
if defined OneDriveConsumer call :remove_shortcut "%OneDriveConsumer%\Desktop\Nexus Harness.lnk"

for /f "usebackq delims=" %%D in (`powershell.exe -NoProfile -NonInteractive -Command "[Environment]::GetFolderPath('Desktop')" 2^>nul`) do call :remove_known_desktop_shortcut "%%D\Nexus Harness.lnk"

if defined APPDATA (
  call :remove_shortcut "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Nexus Harness.lnk"
  call :remove_shortcut "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Nexus Harness\Nexus Harness.lnk"
)

rem scripts/install.ps1 uses this exact version-free per-user directory. Only
rem this constant child of LOCALAPPDATA is eligible for recursive removal.
if defined LOCALAPPDATA call :remove_cli "%LOCALAPPDATA%\Programs\OurHarness"

if "%NEXUS_FAILED%"=="1" goto cleanup_failed

echo.
if "%NEXUS_FOUND%"=="0" (
  echo   Nexus Harness was already absent from this Windows account.
) else if "%NEXUS_DRY_RUN%"=="1" (
  echo   Dry run complete. Nothing was changed.
) else (
  echo   Nexus Harness was removed from this Windows account.
)
echo   Your projects, settings, transcripts, and evidence were preserved.
echo.
if "%NEXUS_QUIET%"=="0" pause
exit /b 0

:uninstall_failed
echo.
echo   The installed app's own uninstaller did not finish successfully.
echo   Nothing else was removed. Close Nexus Harness and run this file again.
echo.
if "%NEXUS_QUIET%"=="0" pause
exit /b 1

:cleanup_failed
echo.
echo   Part of Nexus Harness could not be removed.
echo   Close Nexus Harness, check that these files are not read-only, and run
echo   this file again. Your projects and saved work were not touched.
echo.
if "%NEXUS_QUIET%"=="0" pause
exit /b 1

:run_uninstallers
set "NEXUS_APP_DIR=%~f1"
if not defined NEXUS_APP_DIR exit /b 0
for %%U in ("%NEXUS_APP_DIR%\Uninstall*.exe") do if exist "%%~fU" call :run_one_uninstaller "%%~fU"
if "%NEXUS_FAILED%"=="1" exit /b 1
exit /b 0

:run_one_uninstaller
set "NEXUS_FOUND=1"
echo   Installed app: %~f1
if "%NEXUS_DRY_RUN%"=="1" (
  echo     would run its official uninstaller
  exit /b 0
)
if "%NEXUS_QUIET%"=="1" (
  start "" /wait "%~f1" /S
) else (
  start "" /wait "%~f1"
)
if errorlevel 1 set "NEXUS_FAILED=1"
exit /b 0

:remove_shortcut
if not exist "%~f1" exit /b 0
set "NEXUS_FOUND=1"
echo   Desktop or Start menu shortcut: %~f1
if "%NEXUS_DRY_RUN%"=="1" (
  echo     would remove it
) else (
  del /f /q "%~f1" >nul 2>&1
  if exist "%~f1" set "NEXUS_FAILED=1"
)
exit /b 0

:remove_known_desktop_shortcut
if defined USERPROFILE if /I "%~f1"=="%USERPROFILE%\Desktop\Nexus Harness.lnk" exit /b 0
if defined OneDrive if /I "%~f1"=="%OneDrive%\Desktop\Nexus Harness.lnk" exit /b 0
if defined OneDriveCommercial if /I "%~f1"=="%OneDriveCommercial%\Desktop\Nexus Harness.lnk" exit /b 0
if defined OneDriveConsumer if /I "%~f1"=="%OneDriveConsumer%\Desktop\Nexus Harness.lnk" exit /b 0
call :remove_shortcut "%~f1"
exit /b 0

:remove_cli
set "NEXUS_CLI_ROOT=%~f1"
if not defined NEXUS_CLI_ROOT exit /b 0
if not exist "%NEXUS_CLI_ROOT%\bin\harness.cmd" if not exist "%NEXUS_CLI_ROOT%\app\harness.pyz" exit /b 0
set "NEXUS_FOUND=1"
echo   Command-line installation: %NEXUS_CLI_ROOT%
if "%NEXUS_DRY_RUN%"=="1" (
  echo     would remove it and its exact user PATH entry
  exit /b 0
)

rem Remove only the exact bin directory installed by scripts/install.ps1.
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$target=[IO.Path]::GetFullPath((Join-Path $env:NEXUS_CLI_ROOT 'bin')).TrimEnd('\'); $current=[Environment]::GetEnvironmentVariable('Path','User'); if($null -ne $current){$kept=@($current -split ';' ^| Where-Object { $_ -and ([IO.Path]::GetFullPath($_).TrimEnd('\') -ne $target) }); [Environment]::SetEnvironmentVariable('Path',($kept -join ';'),'User')}" >nul 2>&1
rmdir /s /q "%NEXUS_CLI_ROOT%" >nul 2>&1
if exist "%NEXUS_CLI_ROOT%" set "NEXUS_FAILED=1"
exit /b 0
