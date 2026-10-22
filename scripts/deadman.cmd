@echo off
rem Dead-man check at the run deadline (Task Scheduler): alerts if today's scheduled run has not completed.
cd /d "%~dp0.."
for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do set "%%a=%%b"
".venv\Scripts\python.exe" -m planner.jobs.deadman >> logs\run.log 2>&1
exit /b %ERRORLEVEL%
