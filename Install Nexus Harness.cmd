@echo off
rem ===========================================================================
rem  DOUBLE-CLICK THIS FILE.
rem
rem  It puts a Nexus Harness icon on your desktop. After that you never need
rem  this file, or a terminal, again - you double-click the icon.
rem
rem  It only touches your own account: your desktop, and nothing else. Nothing
rem  here needs an administrator.
rem ===========================================================================

setlocal
cd /d "%~dp0"
title Install Nexus Harness

echo.
echo   Nexus Harness
echo   =============
echo.
echo   This puts a Nexus Harness icon on your desktop.
echo   Nothing outside your own account is touched.
echo.

rem Python, whichever of the two names this machine has it under. Both are
rem looked for, because a machine with one and not the other is common and
rem "python is not recognised" tells nobody which one to go and get.
set "THE_PYTHON="
where py >nul 2>&1 && set "THE_PYTHON=py -3"
if not defined THE_PYTHON where python >nul 2>&1 && set "THE_PYTHON=python"

if not defined THE_PYTHON (
  echo   Python is not on this machine yet, and the harness is written in it.
  echo.
  echo   Get it from https://www.python.org/downloads/ - tick "Add python.exe
  echo   to PATH" while it installs - then double-click this file again.
  echo.
  pause
  exit /b 1
)

%THE_PYTHON% "scripts\put_it_on_your_desktop.py"
set "HOW_IT_WENT=%ERRORLEVEL%"

echo.
if "%HOW_IT_WENT%"=="0" (
  echo   Done. Look on your desktop.
) else (
  echo   That did not work. What it said is above.
)
echo.
pause
exit /b %HOW_IT_WENT%
