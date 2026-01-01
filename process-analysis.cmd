@echo off
cd /d "%~dp0"
python -B -X utf8 -m qianji.analysis process --model qwen3.5:9b-32k --model-limit 20
set "analysis_exit=%errorlevel%"
pause
exit /b %analysis_exit%
