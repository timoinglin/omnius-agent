@echo off
REM Shim so double-click works despite PowerShell execution policy.
REM All logic lives in release.ps1 - re-cuts the rolling GitHub release from pushed main.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0release.ps1" %*
pause
