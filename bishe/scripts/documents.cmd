@echo off
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."
if "%~1"=="" (
  knowledge_graph_backend\.venv\Scripts\python.exe scripts\manage_documents.py list
  echo.
  echo See data\document_library_v1\README.md for import and version commands.
  pause
  exit /b
)
knowledge_graph_backend\.venv\Scripts\python.exe scripts\manage_documents.py %*
