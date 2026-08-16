@echo off
echo Starting Scrim Bot + Dashboard...
start "Scrim Bot" cmd /k "cd /d %~dp0 && python bot.py"
start "Scrim Dashboard" cmd /k "cd /d %~dp0 && python dashboard.py"
echo Both started. Bot runs in one window, dashboard at http://localhost:8080
pause
