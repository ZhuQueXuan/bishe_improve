@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0qdrant.ps1" -Action Install
if errorlevel 1 echo Installation did not complete. Please read the message above.
pause
