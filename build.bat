@echo off
echo ================================
echo  RuneSync Build Script
echo ================================
echo.

:: Clear any previous completion sentinel so a stale result can't fool a
:: poller (e.g. Claude Code's background-task watcher) into thinking we
:: already finished. The sentinel is (re)written only when this run ends.
del /q build_status.txt >nul 2>&1

:: Kill running instances first
echo [1/3] Stopping RuneSync processes...
taskkill /F /IM RuneSync.exe /T >nul 2>&1
:: Legacy watcher (kept around for older installs to clean up)
taskkill /F /IM RuneSyncWatcher.exe /T >nul 2>&1
timeout /t 1 /nobreak >nul

:: Build main app — single-process tray-icon model.
:: The old RuneSyncWatcher.exe is no longer built; League detection
:: happens inside the main process via tray.py's LeaguePoller.
echo [2/3] Building RuneSync.exe...
cd /d "%~dp0"
:: --hidden-import psutil: psutil is imported function-locally in tray.py /
:: lcu.py, so PyInstaller's static analyzer can miss it.
:: pywebview UI (app.py). --collect-all webview pulls the EdgeChromium backend
:: DLLs + JS shim (without it the onefile exe shows a blank window). --hidden-import
:: clr for pythonnet. webui/ is the HTML/CSS/JS/fonts frontend, bundled as data.
if exist icon.ico (
    py -m PyInstaller --noconfirm --clean --onefile --windowed --name "RuneSync" --icon=icon.ico --hidden-import psutil --hidden-import clr --collect-all webview --add-data "webui;webui" --add-data "icon.ico;." --add-data "score_v2\coaching_catalog.json;score_v2" app.py
) else (
    py -m PyInstaller --noconfirm --clean --onefile --windowed --name "RuneSync" --hidden-import psutil --hidden-import clr --collect-all webview --add-data "webui;webui" --add-data "icon.ico;." --add-data "score_v2\coaching_catalog.json;score_v2" app.py
)
if errorlevel 1 (
    echo ERROR: RuneSync build failed!
    >build_status.txt echo FAIL
    :: Only wait for a keypress when launched interactively (double-click).
    :: Automation must pass "nopause" so the script EXITS instead of hanging
    :: forever at "Press any key..." — that hang is what leaves Claude Code
    :: background tasks "running" indefinitely.
    if /i not "%~1"=="nopause" pause
    exit /b 1
)

echo [3/3] Build complete.
>build_status.txt echo OK
echo.
echo ================================
echo  RuneSync.exe is in dist\
echo ================================
:: See note above: skip the interactive pause under automation.
if /i not "%~1"=="nopause" pause
