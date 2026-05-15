@echo off
setlocal
cd /d "%~dp0"

if not exist venv (
    echo Creating virtual environment...
    py -m venv venv
)

call venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt

echo.
echo Starting Device EMS Telemetary Update dashboard...
echo Open your browser to: http://localhost:8502
echo Press Ctrl+C to stop.
echo.
streamlit run app.py --server.port 8502
