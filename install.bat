@echo off
REM Shim so double-click works despite PowerShell execution policy.
REM All logic lives in install.ps1 (idempotent - re-run anytime as a health check).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
pause
