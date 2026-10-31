@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0health-check.ps1"
if errorlevel 1 echo Some services are unavailable. See README for the original startup steps.
pause
