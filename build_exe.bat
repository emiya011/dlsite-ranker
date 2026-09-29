@echo off
rem ============================================================
rem  Build DLsiteRank.exe with PyInstaller
rem
rem  IMPORTANT: keep this file ASCII-only.
rem  cmd.exe parses .bat files using the system ANSI codepage,
rem  so UTF-8 Chinese comments get mangled and break the script.
rem
rem  Requires:  python -m pip install pyinstaller
rem
rem  Output:    dist\DLsiteRank.exe   (single file, no console)
rem
rem  Notes:
rem    --paths libs          customtkinter lives in libs\, not site-packages
rem    --collect-all         bundles its theme json + fonts; without this the
rem                          window comes up unstyled or crashes on start
rem    --hidden-import       websocket-client is imported lazily inside
rem                          ranker._load_websocket(), so PyInstaller cannot
rem                          discover it by scanning. Without this the
rem                          "Login Ci-en" button would fail at runtime.
rem    --add-data icon.ico   the window icon is loaded at runtime; when frozen
rem                          it gets unpacked to sys._MEIPASS
rem    --onefile --windowed  one self-contained exe, no console window
rem
rem  Do NOT ship settings.json with the exe: it may hold a proxy address or
rem  other local preferences. The exe writes a fresh one on first run.
rem ============================================================

setlocal
cd /d "%~dp0"

python -m PyInstaller ^
    --noconfirm --clean ^
    --onefile --windowed ^
    --name DLsiteRank ^
    --icon icon.ico ^
    --paths libs ^
    --collect-all customtkinter ^
    --hidden-import websocket ^
    --add-data "icon.ico;." ^
    --add-data "icon.png;." ^
    ranker_gui.py

if not exist "dist\DLsiteRank.exe" (
    echo.
    echo [ERROR] build failed - dist\DLsiteRank.exe not found
    pause
    exit /b 1
)

echo.
echo [OK] build finished:
echo      %~dp0dist\DLsiteRank.exe
echo.
echo Copy that one file anywhere. On first run it creates
echo settings.json / images\ / reports\ next to itself.
pause
