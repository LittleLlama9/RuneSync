@echo off
cd /d %~dp0
py -m uvicorn main:app --host 0.0.0.0 --port 8000 --no-access-log
