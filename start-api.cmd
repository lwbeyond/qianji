@echo off
cd /d "%~dp0"
python -m qianji serve
if errorlevel 1 pause
