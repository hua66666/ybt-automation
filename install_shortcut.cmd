@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_shortcut.ps1"
if errorlevel 1 (
  echo.
  echo Failed to create the shortcut.
) else (
  echo.
  echo You can now use the shortcut on the desktop.
)
pause
