@echo off
cd /d "%~dp0"
set "DATABASE_URL="
"%~dp0.venv312\Scripts\python.exe" -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
