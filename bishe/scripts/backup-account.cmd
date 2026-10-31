@echo off
cd /d "%~dp0.."
"knowledge_graph_backend\.venv\Scripts\python.exe" -B "scripts\backup-account.py"
echo.
echo Backups contain private account and chat data. Keep them local.
pause
