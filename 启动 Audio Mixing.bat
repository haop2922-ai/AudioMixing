@echo off
rem ===========================================================================
rem  Audio Mixing 启动器（带托盘图标 + 面板窗口 = 能看出它在不在跑）
rem
rem  为什么用这个而不是双击 exe：
rem    这台机器上的安全软件（火绒 HIPS / 游戏加加注入）会拦住"新编译的 exe"
rem    写文件、甚至拦住它注册托盘图标，于是打包版一启动就死。
rem    python.exe 早就在信任名单里，走它启动一切正常。
rem
rem  双击本文件后：右下角出现托盘图标 + 自动弹出面板窗口。
rem  关闭窗口程序继续跑；右键托盘图标可以停止服务并退出。
rem ===========================================================================
setlocal
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 (
  echo.
  echo   [Audio Mixing] 找不到 python，没法启动。
  echo   请安装 Python 3.11+，或者用打包版（前提是安全软件放行过它）。
  echo.
  pause
  exit /b 1
)
echo.
echo   Audio Mixing 正在启动…
echo   右下角会出现托盘图标，面板窗口会自动打开（约 30 秒）
echo.
python audiomixing_launcher.py %*
if errorlevel 1 (
  echo.
  echo   [Audio Mixing] 启动失败，看上面的报错。
  pause
)
endlocal
