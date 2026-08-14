@echo off
REM Open a DESK: a permanent, warm Claude window that Discord can also type
REM into (tools\bridge\desk_bridge.py).  Usage: wakeup-omnius.bat [desk-id]
REM
REM Before 2026-08-02 this opened a plain `claude` tab and Discord mail was
REM answered by a NEW headless process each time - measured at 31s/86s/87s for
REM one-word replies, nearly all of it boot. The bridge keeps ONE warm session
REM in this window, so a reply costs thinking time and nothing else. Sit in
REM this window and work normally: mail is typed in for you while you are
REM away, and never while you are mid-sentence.
title Omnius desk
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\discord\setup.ps1" -IfMissing

set DESK=%1
if "%DESK%"=="" set DESK=orchestrator

where wt >nul 2>nul
if errorlevel 1 goto nowt
REM cmd /c, not /k: the window must go when the desk does, or every restart
REM leaves a dead shell behind (2026-08-02: "daybook opened 2 tabs"). The
REM `|| pause` keeps it open ONLY on a non-zero exit, so a real startup
REM failure stays readable - state\logs\bridge-<desk>.log has it as well.
wt -w 0 new-tab --title "%DESK%" -d "%CD%" cmd /c python tools\bridge\desk_bridge.py %DESK% ^|^| pause
goto end

:nowt
start "%DESK%" cmd /c python tools\bridge\desk_bridge.py %DESK% ^|^| pause

:end
timeout /t 2 >nul
