@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv" (
    echo Creating virtual environment...
    py -3 -m venv .venv
    if errorlevel 1 (
        python -m venv .venv
    )
    if errorlevel 1 (
        echo Could not create .venv.
        echo Install Python 3 with venv support, then run this launcher again.
        pause
        exit /b 1
    )
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt

echo.
echo Starting Device EMS Telemetry Update dashboard...
echo Open your browser to: http://localhost:8502
echo Press Ctrl+C to stop.
echo.
".venv\Scripts\streamlit.exe" run app.py --server.port 8502 --server.headless true
