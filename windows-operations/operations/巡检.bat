@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    py -3 "songuo_order_inspector.py" %*
) else (
    python "songuo_order_inspector.py" %*
)
set "RC=%ERRORLEVEL%"
echo.
pause
endlocal & exit /b %RC%
