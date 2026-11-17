@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=py -3
%PY% --version >nul 2>nul || set PY=python
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo Python bulunamadi. https://www.python.org/downloads/ adresinden kurun, kurulumda "Add python.exe to PATH" kutusunu isaretleyin.
  pause
  exit /b 1
)
if not exist .venv\Scripts\python.exe %PY% -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo Kurulum basarisiz oldu.
  pause
  exit /b 1
)
python server.py
pause
