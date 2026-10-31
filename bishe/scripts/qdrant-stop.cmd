@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0qdrant.ps1" -Action Stop
if errorlevel 1 echo Stop did not complete. Please read the message above.
pause
