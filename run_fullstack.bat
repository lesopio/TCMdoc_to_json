@echo off
setlocal

cd /d "%~dp0"

echo Starting backend in a new window...
start "txt-tojson-backend" cmd /k "cd /d %~dp0 && python server.py"

echo Starting frontend in a new window...
start "txt-tojson-frontend" cmd /k "cd /d %~dp0frontend && npm run dev"

echo Backend:  http://127.0.0.1:8000
echo Frontend: http://127.0.0.1:5173

