@echo off
cd /d "%~dp0"
title Network Manager Launcher

:: Check Administrator Privileges
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Requesting Administrator privileges...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

echo ===================================================
echo    Checking Requirements and Dependencies
echo ===================================================

:: Check Python Installation
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo [!] Python is NOT installed on this system.
    set /p choice_py="Do you want to download and install Python automatically? (Y/N): "
    if /i "%choice_py%"=="Y" (
        echo.
        echo [+] Installing Python via Winget... Please wait...
        winget install -e --id Python.Python.3.11 --accept-package-agreements --accept-source-agreements
        if %errorlevel% neq 0 (
            echo [!] Automatic installation failed. Please install Python manually.
            pause
            exit /b
        )
        echo [+] Python installed successfully!
        echo [!] IMPORTANT: Please close this window and run 'run.bat' again to refresh environment.
        pause
        exit /b
    ) else (
        echo [!] Python is required to run the script. Operation canceled.
        pause
        exit /b
    )
) else (
    echo [+] Python is installed.
)

:: Check psutil Package
python -c "import psutil" >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo [!] Python package 'psutil' is missing.
    set /p choice_pkg="Do you want to install 'psutil>=5.9.0' now? (Y/N): "
    if /i "%choice_pkg%"=="Y" (
        echo.
        echo [+] Installing psutil...
        python -m pip install --upgrade pip >nul 2>&1
        python -m pip install "psutil>=5.9.0"
        if %errorlevel% neq 0 (
            echo [!] Failed to install psutil. Please check your internet connection.
            pause
            exit /b
        )
        echo [+] 'psutil' installed successfully!
    ) else (
        echo [!] 'psutil' is required for network speed and data tracking. Operation canceled.
        pause
        exit /b
    )
) else (
    echo [+] 'psutil' library is installed.
)

:: Check Persian Support Packages (arabic_reshaper and python-bidi)
python -c "import arabic_reshaper; import bidi" >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo [!] Persian support packages 'arabic_reshaper' or 'python-bidi' are missing.
    set /p choice_persian="Do you want to install them now for Persian text support? (Y/N): "
    if /i "%choice_persian%"=="Y" (
        echo.
        echo [+] Installing arabic_reshaper and python-bidi...
        python -m pip install arabic_reshaper python-bidi
        if %errorlevel% neq 0 (
            echo [!] Failed to install Persian support packages. Please check your internet connection.
            pause
            exit /b
        )
        echo [+] Persian packages installed successfully!
    ) else (
        echo [!] Optional packages skipped. Persian text may not display correctly in CMD.
    )
) else (
    echo [+] Persian support libraries are installed.
)

:: Check Required Core Files in Folder
echo.
echo ===================================================
echo    Checking Core System Files
echo ===================================================

if not exist "xray.exe" (
    echo.
    echo [!] ERROR: 'xray.exe' was not found in the current folder!
    echo [!] Please place 'xray.exe' in this directory and try again.
    echo.
    pause
    exit /b
) else (
    echo [+] Found 'xray.exe'.
)

if not exist "wintun.dll" (
    echo.
    echo [!] ERROR: 'wintun.dll' was not found in the current folder!
    echo [!] Please place 'wintun.dll' in this directory for TUN mode support.
    echo.
    pause
    exit /b
) else (
    echo [+] Found 'wintun.dll'.
)

set SCRIPT_FILE=
if exist "network.py" (
    set SCRIPT_FILE=network.py
) else if exist "main.py" (
    set SCRIPT_FILE=main.py
)

if "%SCRIPT_FILE%"=="" (
    echo.
    echo [!] ERROR: Neither 'network.py' nor 'main.py' was found in the current folder!
    echo [!] Please ensure the Python script is located in this directory.
    echo.
    pause
    exit /b
) else (
    echo [+] Found Python script: %SCRIPT_FILE%
)

echo.
echo ===================================================
echo    Starting Network Manager...
echo ===================================================
echo.

start "" python "%SCRIPT_FILE%"