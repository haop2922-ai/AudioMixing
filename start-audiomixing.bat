@echo off
rem ===========================================================================
rem  WaveLink launcher - double-click to start the service.
rem
rem  Default: opens the desktop window (the full panel lives inside it).
rem  Optional: pass "web" to start headless and open the web panel in a browser.
rem
rem  Prefers the packaged dist\AudioMixing.exe (no Python needed). Falls back to
rem  running from source with the system Python.
rem
rem  ASCII-only on purpose: non-ASCII bytes in a .bat display as garbage (or break
rem  execution) on a PC whose OEM code page differs from the author's.
rem ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"

if /i "%~1"=="web" goto :web

if exist "%~dp0dist\AudioMixing.exe" (
  echo [WaveLink] Starting dist\AudioMixing.exe ...
  start "" "%~dp0dist\AudioMixing.exe"
  exit /b 0
)

where python >nul 2>&1
if errorlevel 1 (
  echo [WaveLink] Not found: dist\AudioMixing.exe
  echo [WaveLink] Python is not installed either.
  echo.
  echo   Either build the exe:   python tools\build_exe.py
  echo   or install Python 3.9+  and run:  python audiomixing_launcher.py
  echo.
  pause
  exit /b 1
)

echo [WaveLink] dist\AudioMixing.exe not found - running from source with Python.
echo [WaveLink] The desktop window will open.
start "" pythonw "%~dp0audiomixing_launcher.py"
exit /b 0

:web
rem Headless node plus the web panel in the default browser. Useful on a machine
rem without a desktop, and for checking the browser compatibility of the panel.
if exist "%~dp0dist\AudioMixing.exe" (
  echo [WaveLink] Starting dist\AudioMixing.exe serve ...
  start "" "%~dp0dist\AudioMixing.exe" serve --open-browser
  exit /b 0
)
echo [WaveLink] Starting from source in web mode.
python -m audiomixing serve --open-browser
echo.
echo [WaveLink] Stopped.
pause
