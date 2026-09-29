@echo off
rem ============================================================
rem  PDF to long-image tool - GUI launcher
rem  Double-click this file to open the window.
rem
rem  IMPORTANT: keep this file ASCII-only.
rem  cmd.exe parses .bat with the system ANSI codepage, so
rem  UTF-8 Chinese comments would break the script.
rem ============================================================

cd /d "%~dp0"

set "PYW=%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe"
if exist "%PYW%" goto launch

where pythonw >nul 2>&1
if not errorlevel 1 (
    set "PYW=pythonw"
    goto launch
)

python "%~dp0pdf2img.py"
echo.
echo [INFO] pythonw.exe not found, ran with python instead.
pause
exit /b 0

:launch
rem Self-check first, so a broken import shows an error instead of flashing away.
python -c "import pdf2img" 2>"%~dp0pdf2img_error.log"
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to load pdf2img.py
    echo -------- details --------
    type "%~dp0pdf2img_error.log"
    echo -------------------------
    pause
    exit /b 1
)
del "%~dp0pdf2img_error.log" >nul 2>&1

start "" "%PYW%" "%~dp0pdf2img.py"
exit /b 0
