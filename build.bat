@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo Stopping any running Chande.exe instance before build...
taskkill /F /IM Chande.exe >nul 2>&1
echo Building Chande Windows Desktop Gadget (windowed GUI, no console)...
python -m PyInstaller --noconfirm --onedir --windowed --noconsole --name Chande --icon "assets\icons\chande.ico" --add-data "VERSION;." --add-data "assets;assets" --add-data "translations;translations" main.py
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] PyInstaller build failed.
    exit /b %ERRORLEVEL%
)
if exist "dist\Chande\data" rmdir /S /Q "dist\Chande\data"
if exist "dist\Chande\updates" rmdir /S /Q "dist\Chande\updates"
if exist "dist\Chande\.env" del /F /Q "dist\Chande\.env"
if exist ".env.example" copy /Y ".env.example" "dist\Chande\.env.example" >nul
if exist "VERSION" copy /Y "VERSION" "dist\Chande\VERSION" >nul
if exist "LICENSE" copy /Y "LICENSE" "dist\Chande\LICENSE" >nul
if exist "README.md" copy /Y "README.md" "dist\Chande\README.md" >nul
echo.
echo [SUCCESS] Build complete: dist\Chande\Chande.exe
