@echo off
rem  Scania 2545507 ladder diagnostics - launcher
cd /d "%~dp0"

python -c "import serial" >nul 2>&1
if errorlevel 1 (
    echo.
    echo   pyserial is not installed.
    echo   Run this once:
    echo.
    echo       pip install pyserial
    echo.
    pause
    exit /b 1
)

where pythonw >nul 2>&1 && (
    start "" pythonw "panel_diag.py"
) || (
    start "" python "panel_diag.py"
)
