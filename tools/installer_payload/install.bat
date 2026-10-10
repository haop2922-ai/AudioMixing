@echo off
rem ===========================================================================
rem  Audio Mixing - installer script (runs after the self-extractor unpacks)
rem
rem  What it does:
rem    1. copies the app to  %LOCALAPPDATA%\AudioMixing   (no admin needed)
rem    2. creates a desktop shortcut
rem    3. removes the web mark from the installed files (no SmartScreen prompts)
rem    4. starts the app
rem
rem  Why %LOCALAPPDATA%: no admin prompt, per user, and the files written by this
rem  script carry no "downloaded from the internet" mark, so Windows will not
rem  show the SmartScreen dialog again.
rem ===========================================================================
setlocal EnableExtensions
title Audio Mixing setup

set "SRC=%~dp0"
set "DST=%LOCALAPPDATA%\AudioMixing"

echo.
echo   ==========================================================
echo    Audio Mixing - installing
echo   ==========================================================
echo.
echo   from : %SRC%
echo   to   : %DST%
echo.

if not exist "%DST%" mkdir "%DST%" >nul 2>&1
if not exist "%DST%" (
  echo   [ERROR] cannot create "%DST%"
  pause
  exit /b 1
)

echo   [1/4] copying files ...
xcopy "%SRC%AudioMixing.exe" "%DST%\" /Y /Q >nul 2>&1
if exist "%SRC%AudioMixing-mic.apk" xcopy "%SRC%AudioMixing-mic.apk" "%DST%\" /Y /Q >nul 2>&1
if exist "%SRC%*.md" xcopy "%SRC%*.md" "%DST%\" /Y /Q >nul 2>&1
if not exist "%DST%\AudioMixing.exe" (
  echo   [ERROR] copy failed
  pause
  exit /b 1
)

echo   [2/4] clearing the web mark (prevents SmartScreen prompts) ...
powershell -NoProfile -NonInteractive -Command "Get-ChildItem -LiteralPath '%DST%' -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object { try { Unblock-File -LiteralPath $_.FullName -ErrorAction SilentlyContinue } catch {} }" >nul 2>&1

echo   [3/4] creating the desktop shortcut ...
powershell -NoProfile -NonInteractive -Command "$ws = New-Object -ComObject WScript.Shell; $lnk = $ws.CreateShortcut([Environment]::GetFolderPath('Desktop') + '\Audio Mixing.lnk'); $lnk.TargetPath = '%DST%\AudioMixing.exe'; $lnk.WorkingDirectory = '%DST%'; $lnk.Description = 'Audio Mixing - LAN audio casting'; $lnk.Save()" >nul 2>&1
if exist "%USERPROFILE%\Desktop\Audio Mixing.lnk" (echo         desktop shortcut created) else (echo         [warn] shortcut not created)

echo   [4/4] starting Audio Mixing ...
echo.
echo   ==========================================================
echo    Done.  A "Audio Mixing" shortcut is on your desktop.
echo    First start takes 30-60 seconds (it unpacks itself once).
echo    A tray icon appears bottom-right, then the panel opens.
echo   ==========================================================
echo.
start "" "%DST%\AudioMixing.exe"
timeout /t 6 /nobreak >nul
exit /b 0
