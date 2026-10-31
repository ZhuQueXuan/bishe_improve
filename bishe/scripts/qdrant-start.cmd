@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0qdrant.ps1" -Action Start
if errorlevel 1 echo Startup did not complete. Please read the message above.
pause
