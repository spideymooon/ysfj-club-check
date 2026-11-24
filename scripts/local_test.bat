@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."

echo ============================================================
echo   Youzan daily check-in - local first-login + one-shot test
echo ============================================================

if not exist ".env" (
    echo [i] .env not found, copying from .env.example ...
    copy /Y ".env.example" ".env" >nul
    echo [!] Please edit .env (set PUSHPLUS_TOKEN) then run this script again.
    goto :end
)

REM 优先使用项目内 .venv 的 Python（依赖已装在这里）
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

REM 复用本机已装浏览器：优先 Chrome，其次 Edge；都没有则留空（用 Playwright 内置 Chromium）
set "BROWSER_CHANNEL="
for %%B in (
    "%ProgramFiles%\Google\Chrome\Application\chrome.exe"
    "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
    "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
) do if not defined BROWSER_CHANNEL if exist %%B set "BROWSER_CHANNEL=chrome"
if not defined BROWSER_CHANNEL (
    for %%B in (
        "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
        "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
    ) do if not defined BROWSER_CHANNEL if exist %%B set "BROWSER_CHANNEL=msedge"
)
echo [i] BROWSER_CHANNEL=%BROWSER_CHANNEL%

"%PY%" -m app.main init
if errorlevel 1 goto :fail

echo.
echo [1/3] A browser window will open. Please log in with phone + SMS code.
"%PY%" -m app.main login
if errorlevel 1 goto :fail

echo.
echo [2/3] Running a single check-in test ...
"%PY%" -m app.main once
if errorlevel 1 goto :fail

echo.
echo [3/3] Current status:
"%PY%" -m app.main status

echo.
echo [OK] Local test finished. Next: deploy with Docker (see README).
goto :end

:fail
echo.
echo [!] Step failed. Check the output above and data\logs\app.log
exit /b 1

:end
endlocal
