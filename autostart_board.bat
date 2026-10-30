@echo off
rem Auto-start helper. ASCII only.
rem Usage: Win+R -> shell:startup -> create a shortcut to this file there.
cd /d "%~dp0"
start "board-8768" /min cmd /c start.bat
