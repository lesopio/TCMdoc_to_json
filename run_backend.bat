@echo off
setlocal

cd /d "%~dp0"

echo Starting backend on http://127.0.0.1:8000
python server.py

