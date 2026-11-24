@echo off
chcp 65001 >nul
title 有赞每日自动签到 · 网页控制台
cd /d "%~dp0.."

echo ================================================================
echo   有赞每日自动签到 - 网页控制台
echo ================================================================
echo   启动后请用浏览器打开：http://127.0.0.1:8787
echo   （服务启动后会自动弹出说明；本窗口保持开启，最小化即可）
echo   关闭窗口 = 停止签到服务
echo ================================================================
echo.

REM 优先使用项目内 .venv 的 Python（依赖已装在这里）
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
"%PY%" -m app.main web

echo.
echo 服务已退出。如非有意关闭，请检查上方错误信息。
pause
