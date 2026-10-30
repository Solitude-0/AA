@echo off
rem ASCII only. cmd reads .bat as OEM(GBK); Chinese text here would break commands.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [!] .venv not found. Create it first:
  echo     python -m venv .venv
  echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
  exit /b 1
)

rem Start local PostgreSQL (user-level instance, no admin required)
if exist "E:\pgsql\start_pg.bat" call "E:\pgsql\start_pg.bat"

echo [board] starting on http://127.0.0.1:8768
".venv\Scripts\python.exe" run.py
