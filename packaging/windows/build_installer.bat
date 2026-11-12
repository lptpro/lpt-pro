@echo off
rem Build the Windows installer (LPT-Pro-Setup-<version>.exe) and a test zip on this PC.
rem Needs Python 3.11+; installs PyInstaller (in a private build environment) and Inno Setup
rem (free, via winget) if missing. Output goes to the "dist" folder.
setlocal
cd /d "%~dp0\..\.."
title LPT Pro - build installer

set "PY="
where py >nul 2>nul && py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY ( echo Python 3.11+ is needed to build. & pause & exit /b 1 )

if not exist ".venv-build\Scripts\python.exe" %PY% -m venv .venv-build || (pause & exit /b 1)
set "BPY=.venv-build\Scripts\python.exe"
%BPY% -m pip install -q --upgrade pip
%BPY% -m pip install -q -r requirements.txt pyinstaller pip-licenses || (echo Install failed. & pause & exit /b 1)

for /f %%v in ('%BPY% -c "import lptpro; print(lptpro.__version__)"') do set "VER=%%v"
echo Building LPT Pro %VER% ...

%BPY% -m PyInstaller --noconfirm packaging\lptpro.spec || (echo Build failed. & pause & exit /b 1)
mkdir dist\LPTPro\licenses 2>nul
copy /y LICENSE dist\LPTPro\licenses\ >nul
copy /y THIRD-PARTY-NOTICES.md dist\LPTPro\licenses\ >nul
.venv-build\Scripts\pip-licenses --with-license-file --no-license-path --format=plain-vertical --ignore-packages pyinstaller pyinstaller-hooks-contrib pip-licenses altgraph prettytable wcwidth packaging setuptools pip pefile pywin32-ctypes > dist\LPTPro\licenses\THIRD-PARTY-LICENSES.txt

set "ISCC="
for %%p in ("%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe" "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe") do if exist %%p set "ISCC=%%~p"
if not defined ISCC (
  echo Installing Inno Setup ^(free installer builder^)...
  winget install -e --id JRSoftware.InnoSetup --scope user --accept-package-agreements --accept-source-agreements
  for %%p in ("%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe" "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe") do if exist %%p set "ISCC=%%~p"
)
if not defined ISCC ( echo Inno Setup not found. Get it from https://jrsoftware.org/isdl.php & pause & exit /b 1 )

"%ISCC%" /Q /DAppVersion=%VER% /DDistDir=..\..\dist\LPTPro packaging\windows\lptpro.iss || (echo Installer build failed. & pause & exit /b 1)

set "ZIPDIR=dist\LPT-Pro-%VER%-Windows-Test"
rmdir /s /q "%ZIPDIR%" 2>nul
mkdir "%ZIPDIR%"
copy /y "dist\LPT-Pro-Setup-%VER%.exe" "%ZIPDIR%\" >nul
copy /y "packaging\windows\READ ME FIRST.txt" "%ZIPDIR%\" >nul
powershell -NoProfile -Command "Compress-Archive -Force -Path '%ZIPDIR%\*' -DestinationPath '%ZIPDIR%.zip'"
echo.
echo Done:  %CD%\%ZIPDIR%.zip
explorer /select,"%CD%\%ZIPDIR%.zip"
pause
