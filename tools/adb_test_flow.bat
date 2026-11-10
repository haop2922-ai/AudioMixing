@echo off
rem 双击即可：装测试源 + 抓证据（不会自动放声、不改音量，除非在命令行传 -Volume）
rem 等价命令：powershell -ExecutionPolicy Bypass -File tools\adb_test_flow.ps1 -Volume 8
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0adb_test_flow.ps1" %*
if "%~1"=="" pause
endlocal
