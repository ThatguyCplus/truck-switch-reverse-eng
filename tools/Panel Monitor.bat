@echo off
rem  Scania 2545507 panel monitor - launcher
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

rem pythonw keeps the console window from appearing behind the GUI
where pythonw >nul 2>&1 && (
    start "" pythonw "panel_gui.py"
) || (
    start "" python "panel_gui.py"
)
