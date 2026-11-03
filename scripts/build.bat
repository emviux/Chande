@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0\.."
call "%~dp0\..\build.bat"
exit /b %ERRORLEVEL%
