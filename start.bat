@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 (echo Установите Python 3.10+ & pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install --upgrade -r requirements.txt
if errorlevel 1 (echo Не удалось установить библиотеки & pause & exit /b 1)
start "" http://127.0.0.1:8000
".venv\Scripts\python.exe" app.py
pause
