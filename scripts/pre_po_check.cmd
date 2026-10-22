@echo off
rem Daily pre-PO check (Task Scheduler): load today's SAP exports, then check big draft POs for demand drops.
cd /d "%~dp0.."
for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do set "%%a=%%b"
".venv\Scripts\python.exe" -m planner.ingest >> logs\pre_po.log 2>&1
".venv\Scripts\python.exe" -m planner.jobs.pre_po_check >> logs\pre_po.log 2>&1
exit /b %ERRORLEVEL%
