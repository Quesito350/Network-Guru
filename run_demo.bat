@echo off
:: ============================================================
:: Network Guru - run the simulator demo on this PC
:: ============================================================
:: Installs the Python packages (first run needs internet), builds 30 days of
:: simulated history for the example Mendon site and opens http://127.0.0.1:8080
:: Close this window to stop the dashboard.
:: ============================================================

setlocal
cd /d "%~dp0"

call :find_python
if errorlevel 1 (
    echo [ERROR] Python 3.13 not found. Install it from https://python.org or with winget.
    pause
    exit /b 1
)

echo Installing / checking Python packages ...
%PY% -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo [ERROR] pip install failed. Check the internet connection.
    pause
    exit /b 1
)

set PYTHONUTF8=1
%PY% run.py dev %*
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
