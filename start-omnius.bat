@echo off
title Omnius services
cd /d "%~dp0"

set QUIET=
if /I "%~1"=="/auto" set QUIET=-Quiet
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\discord\setup.ps1" -IfMissing %QUIET%
set NODISCORD=
if errorlevel 2 set NODISCORD=1

set HAVEWD=0
if exist tools\discord\watchdog.py set HAVEWD=1
if defined NODISCORD set HAVEWD=0

where wt >nul 2>nul
if errorlevel 1 goto fallback

REM One window: status banner (left) + daybook (top-right) + watchdog (bottom-right)
if %HAVEWD%==1 goto wt_all
wt -w 0 new-tab --title "omnius" -d "%CD%" cmd /k python tools\status_banner.py --watch ; split-pane -V -d "%CD%\daybook" cmd /k python app.py
goto done

:wt_all
wt -w 0 new-tab --title "omnius" -d "%CD%" cmd /k python tools\status_banner.py --watch ; split-pane -V -d "%CD%\daybook" cmd /k python app.py ; split-pane -H -d "%CD%" cmd /k python tools\discord\watchdog.py
goto done

:fallback
start "omnius-status" /d "%CD%" cmd /k python tools\status_banner.py --watch
start "daybook" /min /d "%CD%\daybook" cmd /k python app.py
if %HAVEWD%==1 start "watchdog" /min /d "%CD%" cmd /k python tools\discord\watchdog.py
echo [i] Windows Terminal not found - services in separate windows

:done
echo [OK] Omnius services starting - the status panel shows live state
if defined NODISCORD echo [i] Discord not configured - local mode (banner explains)
echo [i] want the orchestrator right now? run wakeup-omnius.bat
timeout /t 4 >nul
