@echo off
rem ASCII only. Kill whatever is listening on the board port.
setlocal
set PORT=8768
set FOUND=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
  echo [board] killing PID %%a
  taskkill /PID %%a /F >nul 2>&1
  set FOUND=1
)
if "%FOUND%"=="0" echo [board] nothing listening on %PORT%
