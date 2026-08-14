@echo off
title Notes - setup
cd /d "%~dp0"

echo.
echo   Notes - setup
echo   =============
echo.

rem ---------------------------------------------------------------- python
where python >nul 2>nul
if errorlevel 1 (
    echo   [X] Python was not found in PATH.
    echo.
    echo       Install Python 3.10 or newer from https://python.org
    echo       During setup, tick "Add python.exe to PATH", then run this again.
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%V in ('python --version 2^>^&1') do echo   [ok] found %%V

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo   [X] That Python is too old - Notes needs 3.10 or newer.
    echo       Get a current version from https://python.org and run this again.
    echo.
    pause
    exit /b 1
)
echo   [ok] version is 3.10 or newer

rem ------------------------------------------------------------ packages
rem Notes has no third-party dependencies, so this installs nothing today.
rem It is wired up so that if requirements.txt ever gains a real entry, this
rem file already handles it. A broken pip is therefore not fatal here.
echo   [..] checking packages
python -m pip install -r requirements.txt --quiet --disable-pip-version-check 2>nul
if errorlevel 1 (
    echo   [ok] no packages needed ^(pip unavailable, but Notes is stdlib-only^)
) else (
    echo   [ok] no packages needed ^(Notes is stdlib-only^)
)

rem -------------------------------------------------------------- verify
python -c "import app" 2>nul
if errorlevel 1 (
    echo   [X] app.py could not be loaded - the copy may be incomplete.
    python -c "import app"
    echo.
    pause
    exit /b 1
)
echo   [ok] app.py loads

if not exist "notes" mkdir "notes"
echo   [ok] notes folder ready

rem ------------------------------------------------------------ self-test
rem Runs against a temporary folder - it never touches your real notes.
echo   [..] running self-test
python test_storage.py > "%TEMP%\notes-selftest.log" 2>&1
if errorlevel 1 (
    echo   [X] self-test failed:
    echo.
    type "%TEMP%\notes-selftest.log"
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%L in ('findstr /b /c:"all " "%TEMP%\notes-selftest.log"') do echo   [ok] self-test: %%L
del "%TEMP%\notes-selftest.log" >nul 2>nul

rem ---------------------------------------------------------------- done
echo.
echo   Setup complete. Double-click run.bat to start Notes.
echo   It will open http://localhost:5111 in your browser.
echo.
pause
