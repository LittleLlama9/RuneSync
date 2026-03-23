@echo off
echo ================================
echo  RuneSync Build Script
echo ================================
echo.

:: Kill running instances first
echo [1/4] Stopping RuneSync processes...
taskkill /F /IM RuneSync.exe /T >nul 2>&1
taskkill /F /IM RuneSyncWatcher.exe /T >nul 2>&1
timeout /t 1 /nobreak >nul

:: Build main app
echo [2/4] Building RuneSync.exe...
cd /d C:\Users\Matth\RuneSync
if exist icon.ico (
    py -m PyInstaller --noconfirm --onefile --windowed --name "RuneSync" --icon=icon.ico main.py
) else (
    py -m PyInstaller --noconfirm --onefile --windowed --name "RuneSync" main.py
)
if errorlevel 1 (
    echo ERROR: RuneSync build failed!
    pause
    exit /b 1
)

:: Build watcher
echo [3/4] Building RuneSyncWatcher.exe...
if exist icon.ico (
    py -m PyInstaller --noconfirm --onefile --noconsole --name "RuneSyncWatcher" --icon=icon.ico watcher.py
) else (
    py -m PyInstaller --noconfirm --onefile --noconsole --name "RuneSyncWatcher" watcher.py
)
if errorlevel 1 (
    echo ERROR: Watcher build failed!
    pause
    exit /b 1
)

:: Restart watcher
echo [4/4] Restarting watcher...
start "" "C:\Users\Matth\RuneSync\dist\RuneSyncWatcher.exe"

echo.
echo ================================
echo  Build complete!
echo  RuneSync.exe and RuneSyncWatcher.exe are in dist\
echo ================================
pause
