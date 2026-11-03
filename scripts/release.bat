@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0\.."

set /p APP_VERSION=<VERSION
set "APP_VERSION=%APP_VERSION: =%"
if "%APP_VERSION%"=="" set "APP_VERSION=1.2.0"

echo ============================================================================
echo Building Chande Release Package v%APP_VERSION% (Windows x64)
echo ============================================================================

if /I not "%~1"=="--package-only" (
    call "%~dp0\..\build.bat"
    if !ERRORLEVEL! NEQ 0 (
        echo [ERROR] Build step failed.
        exit /b !ERRORLEVEL!
    )
)

:: Ensure zero user data or secrets in dist\Chande before archiving
if exist "dist\Chande\data" rmdir /S /Q "dist\Chande\data"
if exist "dist\Chande\updates" rmdir /S /Q "dist\Chande\updates"
if exist "dist\Chande\.env" del /F /Q "dist\Chande\.env"
if exist ".env.example" copy /Y ".env.example" "dist\Chande\.env.example" >nul
if exist "VERSION" copy /Y "VERSION" "dist\Chande\VERSION" >nul
if exist "LICENSE" copy /Y "LICENSE" "dist\Chande\LICENSE" >nul
if exist "README.md" copy /Y "README.md" "dist\Chande\README.md" >nul

set "ZIP_NAME=Chande-v%APP_VERSION%-Windows-x64.zip"
set "ZIP_PATH=dist\%ZIP_NAME%"
set "SHA_PATH=%ZIP_PATH%.sha256"
set "SUMS_PATH=dist\SHA256SUMS.txt"

echo Packaging %ZIP_PATH% and generating SHA-256 checksums...
python -c "import hashlib, pathlib, zipfile; src = pathlib.Path(r'dist/Chande'); zpath = pathlib.Path(r'%ZIP_PATH%'); zpath.unlink(missing_ok=True); zf = zipfile.ZipFile(zpath, 'w', zipfile.ZIP_DEFLATED, compresslevel=6); [zf.write(p, pathlib.Path('Chande') / p.relative_to(src)) for p in sorted(src.rglob('*')) if p.is_file()]; zf.close(); digest = hashlib.sha256(zpath.read_bytes()).hexdigest().lower(); line = f'{digest}  %ZIP_NAME%\n'; pathlib.Path(r'%SHA_PATH%').write_text(line, encoding='ascii'); pathlib.Path(r'%SUMS_PATH%').write_text(line, encoding='ascii'); print(f'[SHA-256] {line.strip()}')"

if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Failed to create release archive or checksum.
    exit /b %ERRORLEVEL%
)

echo.
echo [SUCCESS] Release artifacts ready:
echo   - %ZIP_PATH%
echo   - %SHA_PATH%
echo   - %SUMS_PATH%
