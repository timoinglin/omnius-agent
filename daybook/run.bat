@echo off
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found in PATH. Install it from https://python.org and run this again.
    pause
    exit /b 1
)

rem ask the app which port it will use, so config.ini / PORT are respected
set "NOTES_PORT=5111"
for /f "usebackq delims=" %%p in (`python -c "import app; print(app.PORT)" 2^>nul`) do set "NOTES_PORT=%%p"
set "NOTES_URL=http://localhost:%NOTES_PORT%"

title Notes - %NOTES_URL%

rem already running? just open the browser
curl -s -o nul --max-time 1 %NOTES_URL%/api/months >nul 2>nul
if not errorlevel 1 (
    echo Notes is already running - opening the browser.
    start %NOTES_URL%
    exit /b 0
)

rem open the browser once the server has had a moment to bind
rem (ping is the delay: unlike timeout it needs no console stdin)
start "" /b cmd /c "ping -n 2 127.0.0.1 >nul & start %NOTES_URL%"

echo Starting Notes at %NOTES_URL%  (close this window or press Ctrl+C to stop)
python app.py

rem keep the window open if the server exited with an error
if errorlevel 1 pause
