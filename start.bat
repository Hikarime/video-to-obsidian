@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw -X utf8 "%~dp0app.py" %*
    exit /b
)
where pyw >nul 2>nul
if not errorlevel 1 (
    start "" pyw -3 -X utf8 "%~dp0app.py" %*
    exit /b
)
where python >nul 2>nul
if not errorlevel 1 (
    python -X utf8 "%~dp0app.py" %*
    exit /b
)
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -X utf8 "%~dp0app.py" %*
    exit /b
)
echo Python 3.10 or newer is required. Add Python to PATH and try again.
echo.
pause
endlocal
