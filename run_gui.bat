@echo off
rem ============================================================
rem  DLsite Ranking Scraper - GUI launcher
rem  Double-click this file to open the window.
rem
rem  IMPORTANT: keep this file ASCII-only.
rem  cmd.exe parses .bat files using the system ANSI codepage,
rem  so UTF-8 Chinese comments get mangled and break the script.
rem ============================================================

cd /d "%~dp0"

set "PYW=%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe"
if exist "%PYW%" goto launch

where pythonw >nul 2>&1
if not errorlevel 1 (
    set "PYW=pythonw"
    goto launch
)

rem No pythonw found: run with python and keep the window open.
python "%~dp0ranker_gui.py"
echo.
echo [INFO] pythonw.exe not found, ran with python instead.
pause
exit /b 0

:launch
rem Self-check first: if the script cannot even be imported,
rem show the error instead of silently flashing away.
python -c "import ranker_gui" 2>"%~dp0gui_error.log"
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to load ranker_gui.py
    echo -------- details --------
    type "%~dp0gui_error.log"
    echo -------------------------
    pause
    exit /b 1
)
del "%~dp0gui_error.log" >nul 2>&1

start "" "%PYW%" "%~dp0ranker_gui.py"
exit /b 0
