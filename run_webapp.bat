@echo off
setlocal
cd /d "%~dp0"
if not exist venv\Scripts\python.exe (
  echo Creating venv...
  py -3 -m venv venv || python -m venv venv
)
venv\Scripts\python.exe -c "import flask, jinja2" 2>nul || venv\Scripts\python.exe -m pip install -r requirements-console.txt
start "" http://127.0.0.1:5460
venv\Scripts\python.exe webapp.py
