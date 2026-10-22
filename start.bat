@echo off
cd /d "%~dp0"

where python >nul 2>nul || (
    echo Python not found. Install it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during install.
    pause
    exit /b 1
)

python -c "import yt_dlp, youtube_transcript_api" 2>nul || (
    echo Installing dependencies, first run only...
    python -m pip install -r requirements.txt || (pause & exit /b 1)
)

rem pythonw = no console window. Errors go to yt_hotspots.log.
start "" pythonw yt_hotspots.py
