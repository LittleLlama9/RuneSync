@echo off
echo ================================
echo  RuneSync Build Script
echo ================================
echo.

:: Kill running instances first
echo [1/5] Stopping RuneSync processes...
taskkill /F /IM RuneSync.exe /T >nul 2>&1
taskkill /F /IM RuneSyncWatcher.exe /T >nul 2>&1
timeout /t 1 /nobreak >nul

:: Build main app
echo [2/5] Building RuneSync.exe...
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

:: Build watcher
echo [3/5] Building RuneSyncWatcher.exe...
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

:: Copy server folder to dist
echo [4/5] Copying server to dist\server...
if exist "dist\server" rmdir /s /q "dist\server"
xcopy /E /I /Q server dist\server

:: Restart watcher
echo [5/5] Restarting watcher...
start "" "%~dp0dist\RuneSyncWatcher.exe"

echo.
echo ================================
echo  Build complete!
echo  RuneSync.exe and RuneSyncWatcher.exe are in dist\
echo ================================
pause
