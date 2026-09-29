@echo off
setlocal
cd /d "%~dp0"
python src\run.py
set EXITCODE=%ERRORLEVEL%
echo.
if not "%EXITCODE%"=="0" echo JobRadar exited with code %EXITCODE%.
pause
exit /b %EXITCODE%
