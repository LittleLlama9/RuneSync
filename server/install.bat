@echo off
echo Installing RuneSync Server dependencies...
cd /d %~dp0
py -m pip install -r requirements.txt
py -m playwright install chromium
echo.
echo Done. Run start_server.bat to launch.
pause
