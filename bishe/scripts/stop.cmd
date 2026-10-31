@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1"
if errorlevel 1 echo Stop did not complete. Read the message above.
pause
