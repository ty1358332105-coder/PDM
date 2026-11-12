@echo off
rem SinoCC PDM Query Tool launcher (ASCII only to avoid codepage garbling)
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if exist "%PY%" goto run

set "PY=%~dp0..\sinocc-pdm-bridge\.venv\Scripts\python.exe"
if exist "%PY%" goto run

set "PY=python"

:run
"%PY%" run.py %*
if errorlevel 1 pause
endlocal
