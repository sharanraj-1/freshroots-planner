@echo off
rem Twice-weekly planning run (Windows Task Scheduler). Ingests %SAP_INBOUND_DIR%, then plans today's date.
cd /d "%~dp0.."
for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do set "%%a=%%b"
".venv\Scripts\python.exe" -m planner.jobs.run >> logs\run.log 2>&1
exit /b %ERRORLEVEL%
