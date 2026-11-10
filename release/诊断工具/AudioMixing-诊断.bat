@echo off
setlocal
echo.
echo  Audio Mixing - Remote Diagnostic
echo  -------------------------------
echo  This will check the PC and create a report in about one minute.
echo  Notepad will open it automatically. Please send that file back.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0diagnose.ps1"
echo.
echo  Done. If Notepad did not open, look for "AudioMixing-*.txt" in this folder.
echo.
pause
