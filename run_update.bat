@echo off
REM Run this from Windows Task Scheduler (e.g. daily at 6:00 AM, Sept-Nov).
REM Set the folder below to wherever you put this project, ideally inside OneDrive
REM so Power BI Service can refresh from the CSVs without a gateway.
cd /d "%~dp0"
python update.py >> data\update_log.txt 2>&1
