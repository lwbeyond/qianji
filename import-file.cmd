@echo off
cd /d "%~dp0"
if "%~1"=="" (
  echo Drag a Qianji JSON export onto this file to import it.
  pause
  exit /b 1
)
python -m qianji import "%~1"
set "import_exit=%errorlevel%"
pause
exit /b %import_exit%
