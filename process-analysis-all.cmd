@echo off
cd /d "%~dp0"
python -B -X utf8 -m qianji.batch_analysis
set "analysis_exit=%errorlevel%"
pause
exit /b %analysis_exit%
