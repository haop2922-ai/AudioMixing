@echo off
rem ===========================================================================
rem  Audio Mixing - start from source
rem
rem  为什么有这个东西：
rem    有些安全软件（火绒的 HIPS、游戏加加的注入）会拦住【刚编译出来的新 exe】
rem    写任何文件，于是打包版一启动就死：
rem        PermissionError: [WinError 5] 拒绝访问
rem        [PYI] Could not create temporary directory!
rem    而 python.exe 通常早就在信任名单里，所以从源码启动能正常跑。
rem
rem  用法：双击即可。想传参数就接在后面（serve / net / devices ...）
rem ===========================================================================
setlocal
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 (
  echo [Audio Mixing] not found: python. Install Python 3.11+ or use the packaged exe.
  pause
  exit /b 1
)
echo.
echo   Audio Mixing - starting from source
echo   panel: http://127.0.0.1:17800   (close this window to stop)
echo.
python -m audiomixing serve --open-browser %*
if errorlevel 1 (
  echo.
  echo [Audio Mixing] failed - see the message above.
  pause
)
endlocal
