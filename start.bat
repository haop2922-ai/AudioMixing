@echo off
rem ===========================================================================
rem  AudioMixing launcher
rem
rem  Order:
rem    1) dist_nuitka\AudioMixing.exe  (Nuitka onefile build)
rem    2) dist\AudioMixing.exe         (legacy build output)
rem    3) source via python audiomixing_launcher.py
rem
rem  Usage:
rem    start.bat            start normally (tray icon + panel window)
rem    start.bat web        headless node + panel in the browser
rem
rem  ASCII-only on purpose: non-ASCII bytes in a .bat display as garbage (or
rem  break execution) on a PC whose OEM code page differs from the author's.
rem ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "EXE=%CD%\dist_nuitka\AudioMixing.exe"
if not exist "%EXE%" set "EXE=%CD%\dist\AudioMixing.exe"
if not exist "%EXE%" set "EXE="

if /i "%~1"=="web" (
  set "ARGS=serve --open-browser"
) else (
  set "ARGS=%*"
)

if defined EXE (
  echo [AudioMixing] starting "%EXE%" %ARGS%
  start "" "%EXE%" %ARGS%
  exit /b 0
)

where python >nul 2>&1
if errorlevel 1 (
  echo [AudioMixing] No packaged exe found, and Python is not installed.
  echo   Build the exe: python tools\build_nuitka.py
  echo   or install Python 3.11+ and run start.bat again.
  pause
  exit /b 1
)

echo [AudioMixing] No packaged exe found - starting from source.
python audiomixing_launcher.py %ARGS%
if errorlevel 1 pause
endlocal
