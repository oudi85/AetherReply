@echo off
setlocal
set "SCRIPT=%~dp0installer\install.ps1"
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%SCRIPT%" %*
if errorlevel 1 (
  echo.
  echo Installation did not complete. Check the message above.
  pause
)
