@echo off
setlocal
cd /d %~dp0

if not exist ".venv\Scripts\python.exe" (
  echo Local virtual environment not found: .venv
  echo Create it with: python -m venv .venv
  exit /b 1
)

if not exist ".venv\Scripts\pyinstaller.exe" (
  echo PyInstaller not found in .venv
  echo Install it with: .venv\Scripts\python.exe -m pip install -r requirements-build.txt
  exit /b 1
)

echo Stopping any running WSLAgentMonitor.exe processes...
powershell -NoProfile -Command "Stop-Process -Name WSLAgentMonitor -Force -ErrorAction SilentlyContinue"

set WAIT_COUNT=0
:wait_for_process_exit
powershell -NoProfile -Command "if (Get-Process WSLAgentMonitor -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }"
if errorlevel 1 (
  set /a WAIT_COUNT+=1
  if %WAIT_COUNT% GEQ 20 (
    echo Could not stop WSLAgentMonitor.exe. Please close it and try again.
    exit /b 1
  )
  timeout /t 1 /nobreak >nul
  goto wait_for_process_exit
)

if exist "dist\WSLAgentMonitor.exe" set WAIT_COUNT=0
:wait_for_exe_release
if exist "dist\WSLAgentMonitor.exe" (
  del /f /q "dist\WSLAgentMonitor.exe" >nul 2>nul
  if exist "dist\WSLAgentMonitor.exe" (
    set /a WAIT_COUNT+=1
    if %WAIT_COUNT% GEQ 20 (
      echo Could not release dist\WSLAgentMonitor.exe. Please close any app using it and try again.
      exit /b 1
    )
    timeout /t 1 /nobreak >nul
    goto wait_for_exe_release
  )
)

".venv\Scripts\python.exe" -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --name WSLAgentMonitor ^
  app.py

if errorlevel 1 (
  echo.
  echo Build failed.
  exit /b 1
)

echo.
echo Build complete:
echo dist\WSLAgentMonitor.exe
exit /b 0
