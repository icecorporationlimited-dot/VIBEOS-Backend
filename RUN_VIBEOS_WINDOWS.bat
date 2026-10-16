@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 run_webapp.py
) else (
  python run_webapp.py
)
pause
