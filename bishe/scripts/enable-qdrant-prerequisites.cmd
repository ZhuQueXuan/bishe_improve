@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0enable-qdrant-prerequisites.ps1"
if errorlevel 1 pause
