@echo off
REM Shim so double-click works despite PowerShell execution policy.
REM All logic lives in pack.ps1 - builds the portable omnius-<date>.zip next to this folder.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0pack.ps1" %*
pause
