@echo off
REM Shim so double-click works despite PowerShell execution policy.
REM All logic lives in stop-omnius.ps1 - stops services, disables the
REM self-healing tasks, and reports anything that survived.
title Omnius - stop
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-omnius.ps1" %*
pause
