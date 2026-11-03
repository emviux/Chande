@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if exist "%~dp0dist\Chande\Chande.exe" (
    start "" "%~dp0dist\Chande\Chande.exe" %*
    exit /b 0
)
where pythonw >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    start "" pythonw "%~dp0main.py" %*
) else (
    start "" python "%~dp0main.py" %*
)
