@echo off
echo ================================
echo  RuneSync Build Script
echo ================================
echo.

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
if exist icon.ico (
    py -m PyInstaller --noconfirm --clean --onefile --windowed --name "RuneSync" --icon=icon.ico --add-data "assets/spells;assets/spells" --add-data "icon.ico;." main.py
) else (
    py -m PyInstaller --noconfirm --clean --onefile --windowed --name "RuneSync" --add-data "assets/spells;assets/spells" --add-data "icon.ico;." main.py
)
if errorlevel 1 (
    echo ERROR: RuneSync build failed!
    pause
    exit /b 1
)

echo [3/3] Build complete.
echo.
echo ================================
echo  RuneSync.exe is in dist\
echo ================================
pause
