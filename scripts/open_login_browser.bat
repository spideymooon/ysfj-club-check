@echo off
chcp 65001 >nul
title 手动登录窗口（不会自动关闭）
cd /d "%~dp0.."

echo ================================================================
echo   用一个普通浏览器窗口登录（程序不接管、不自动关闭）
echo   登录完成后，直接关闭这个浏览器窗口；登录态会留在
echo   data\browser_profile 里，回到控制台点「立即签到」即可验证。
echo ================================================================
echo.

set "EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not exist "%EDGE%" set "EDGE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if not exist "%EDGE%" (
    echo [!] 没找到 Edge，请手动用任意浏览器打开下面地址：
    echo     https://club.ysjf.com/wscump/checkin/result?kdt_id=149536603
    pause
    exit /b 1
)

"%EDGE%" --user-data-dir="%CD%\data\browser_profile" --no-first-run --no-default-browser-check "https://club.ysjf.com/wscump/checkin/result?kdt_id=149536603"

echo.
echo 浏览器已关闭。回到 http://127.0.0.1:8787 点「立即签到」验证登录态。
pause
