@echo off
setlocal
cd /d "%~dp0"
title Device EMS Console
if not exist venv\Scripts\python.exe (
  echo Creating the Python environment ...
  py -3 -m venv venv 2>nul || python -m venv venv
  if not exist venv\Scripts\python.exe (
    echo Could not create venv. Install Python 3.10+ from python.org and run this again.
    pause
    exit /b 1
  )
)
venv\Scripts\python.exe -c "import flask, jinja2" 2>nul || venv\Scripts\python.exe -m pip install -r requirements.txt
echo Starting the console -- the browser opens when it is ready. Close this window to stop it.
venv\Scripts\python.exe webapp.py --open-browser
echo.
echo The console stopped. Read any error above.
pause
