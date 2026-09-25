@echo off
setlocal
set "SCRIPT=%~dp0installer\uninstall.ps1"
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%SCRIPT%" %*
if errorlevel 1 (
  echo.
  echo Uninstall did not complete. Check the message above.
  pause
)
