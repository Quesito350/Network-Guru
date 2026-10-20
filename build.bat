@echo off
:: ============================================================
:: Network Guru - Build Script
:: ============================================================
:: 1. Python + dependencies (incl. pytest)
:: 2. Linux wheels for the server image (deploy\wheels, checked against deploy\requirements.lock)
:: 3. Tests (tests\)
:: 4. ..\NetworkGuru_v<version>.zip + .sha256 (release.py; older zips -> ..\archive\)
:: 5. Publish: commit, tag v<version>, push to GitHub, GitHub Release
::    (build only, no publishing:  build.bat nopublish)
::
:: Before building a new version: bump __version__ in networkguru\__init__.py and add
:: an entry at the top of networkguru\changelog.py (release.py refuses otherwise).
:: ============================================================

setlocal
cd /d "%~dp0"
set "PUBLISH=--publish"
if /i "%~1"=="nopublish" set "PUBLISH="

echo.
echo ============================================================
echo   Network Guru - Build
echo ============================================================
echo.

call :find_python
if errorlevel 1 (
    echo [ERROR] Python not found.
    pause
    exit /b 1
)
echo Using Python: %PY%

echo [1/4] Installing dependencies ...
%PY% -m pip install --quiet -r requirements-dev.txt
if errorlevel 1 (
    echo [ERROR] pip install failed.  Check your internet connection.
    pause
    exit /b 1
)

echo [2/4] Linux wheels for the server image ...
%PY% tools\fetch_wheels.py --check >nul 2>&1
if errorlevel 1 (
    %PY% tools\fetch_wheels.py
    if errorlevel 1 (
        echo [ERROR] Could not fetch the Linux wheels.  Check your internet connection.
        pause
        exit /b 1
    )
)

echo [3/4] Running tests ...
set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1
%PY% -m pytest
if errorlevel 1 (
    echo.
    echo [ERROR] Tests failed - nothing packaged.
    pause
    exit /b 1
)

echo [4/4] Packaging the release zip %PUBLISH% ...
%PY% release.py %PUBLISH%
if errorlevel 1 (
    echo.
    echo [ERROR] Release packaging failed.  See output above.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Build complete!  -^> ..\NetworkGuru_v^<version^>.zip
echo ============================================================
echo.
endlocal
pause
exit /b 0

:find_python
set "PY=python"
python --version >nul 2>&1
if not errorlevel 1 exit /b 0
set "PY=py -3"
py -3 --version >nul 2>&1
if not errorlevel 1 exit /b 0
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if exist "%%~D\python.exe" (
        set PY="%%~D\python.exe"
        exit /b 0
    )
)
exit /b 1
