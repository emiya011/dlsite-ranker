@echo off
rem ============================================================
rem  DLsite Ranking Scraper - scheduled run entry
rem  Intended to be called by Windows Task Scheduler.
rem
rem  IMPORTANT: keep this file ASCII-only.
rem  cmd.exe parses .bat files using the system ANSI codepage,
rem  so UTF-8 Chinese comments get mangled and break the script.
rem
rem  Default behaviour (no switches needed):
rem    fetch exact rating + download cover image + build HTML report.
rem    CSV is NOT written; add --csv if you want it.
rem
rem  Options used below:
rem    --period     day / week / month / year
rem    --category   comic / game / voice (game covers games and videos)
rem    --limit      top N per ranking
rem    --image-dir  where cover images go
rem    --out        csv path; also decides the workdir
rem                 (the HTML report goes to <workdir>\reports\)
rem
rem  To skip the slow steps: --no-precise-rating  --no-images  --no-html
rem ============================================================

setlocal
cd /d "%~dp0"
if not exist "logs" mkdir "logs"

rem Uncomment and edit if a proxy is needed:
rem set HTTPS_PROXY=http://127.0.0.1:7890

"C:\Users\12590\AppData\Local\Programs\Python\Python310\python.exe" ranker.py ^
    --period week ^
    --category game ^
    --limit 50 ^
    --image-dir "%~dp0images" ^
    --out "%~dp0rank.csv" ^
    >> "%~dp0logs\run.log" 2>&1

echo [%date% %time%] exit=%ERRORLEVEL% >> "%~dp0logs\run.log"
endlocal
