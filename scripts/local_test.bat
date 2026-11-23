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

REM Windows has Chrome installed; reuse it, no need to download Chromium.
set "BROWSER_CHANNEL=chrome"

python -m app.main init
if errorlevel 1 goto :fail

echo.
echo [1/3] A Chrome window will open. Please log in with phone + SMS code.
python -m app.main login
if errorlevel 1 goto :fail

echo.
echo [2/3] Running a single check-in test ...
python -m app.main once
if errorlevel 1 goto :fail

echo.
echo [3/3] Current status:
python -m app.main status

echo.
echo [OK] Local test finished. Next: deploy with Docker (see README).
goto :end

:fail
echo.
echo [!] Step failed. Check the output above and data\logs\app.log
exit /b 1

:end
endlocal
