@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" backend\app.py
) else (
    py backend\app.py
)
pause
