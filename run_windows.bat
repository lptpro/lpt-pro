@echo off
rem LPT Pro - run from source on Windows.
rem Sets up a private Python environment in .venv the first time, then starts the app.
setlocal
cd /d "%~dp0"
title LPT Pro - setup

rem --- Python ---------------------------------------------------------------------
set "PY="
where py >nul 2>nul && py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && python --version >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python is not installed. LPT Pro needs Python 3.11 or newer.
  echo.
  choice /M "Install Python 3.13 now (official package, via winget)"
  if errorlevel 2 (
    echo Get it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    pause & exit /b 1
  )
  winget install -e --id Python.Python.3.13 --scope user --accept-package-agreements --accept-source-agreements
  echo.
  echo Python is installed. Close this window and double-click run_windows.bat again.
  pause & exit /b 0
)

rem --- Apple's USB service (comes with the free Apple Devices app) ----------------
sc query "Apple Mobile Device Service" >nul 2>nul
if errorlevel 1 (
  echo.
  echo  IMPORTANT: Apple's iPhone USB service was not found on this PC.
  echo  LPT Pro needs it to talk to an iPhone over the USB cable.
  echo  Install the free "Apple Devices" app from the Microsoft Store.
  echo  iTunes is NOT required and not recommended.
  echo.
  start "" "ms-windows-store://pdp/?productid=9NP83LWLPZ9K"
  echo  The Store page is opening. LPT Pro will start anyway and connect once it is installed.
  echo.
)

rem --- private environment with the required packages ----------------------------
if not exist ".venv\Scripts\python.exe" (
  echo First run: setting things up. This takes a few minutes, only once...
  %PY% -m venv .venv || (echo Could not create the environment. & pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt -q || (echo Install failed - see messages above. & pause & exit /b 1)

rem --- desktop shortcut (opens the app directly, no console window) ----------------
powershell -NoProfile -Command "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut($d+'\LPT Pro.lnk'); $s.TargetPath='%CD%\.venv\Scripts\pythonw.exe'; $s.Arguments='-m lptpro'; $s.WorkingDirectory='%CD%'; $s.IconLocation='%CD%\lptpro\icon.ico'; $s.Description='Copy photos and videos from your iPhone'; $s.Save()" >nul 2>nul && echo A shortcut "LPT Pro" is now on your desktop.

if /I "%1"=="check" (
  ".venv\Scripts\python.exe" -m lptpro --check
  pause & exit /b
)
if /I "%1"=="debug" (
  ".venv\Scripts\python.exe" -m lptpro
  pause & exit /b
)
start "" ".venv\Scripts\pythonw.exe" -m lptpro
