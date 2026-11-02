@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (
  py -m venv .venv
  if errorlevel 1 python -m venv .venv
  if errorlevel 1 (
    echo Python не найден. Установите Python 3.10 или новее.
    pause
    exit /b 1
  )
)
call .venv\Scripts\activate.bat
python -m pip install --quiet -r requirements.txt
if errorlevel 1 (
  echo Не удалось установить зависимости.
  pause
  exit /b 1
)
start "" http://127.0.0.1:8000
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
