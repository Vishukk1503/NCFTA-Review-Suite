@echo off
setlocal EnableExtensions DisableDelayedExpansion
title NCFTA Review Suite - Work PC setup
echo NCFTA Review Suite - Work PC setup
echo Starting installer from "%~dp0"
echo.
set "NCFTA_INSTALL_LOG=%TEMP%\NCFTA-Install-%RANDOM%-%RANDOM%.log"
set "NCFTA_INSTALL_STARTUP_LOG=%NCFTA_INSTALL_LOG%.startup.txt"
set "NCFTA_INSTALL_STATUS_FILE=%NCFTA_INSTALL_LOG%.status"
>"%NCFTA_INSTALL_STARTUP_LOG%" echo NCFTA installer launcher started.
set "RESULT=1"
if not exist "%~dp0Install-Work-PC.ps1" (
    echo ERROR: Install-Work-PC.ps1 is missing beside this BAT file.
    echo Extract ALL files from the downloaded ZIP before running setup.
    goto failed
)
set "NCFTA_POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if defined PROCESSOR_ARCHITEW6432 set "NCFTA_POWERSHELL=%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%NCFTA_POWERSHELL%" (
    echo ERROR: Windows PowerShell was not found. Ask IT to check Windows PowerShell access.
    goto failed
)
echo Launching Windows PowerShell. Setup progress should appear below.
"%NCFTA_POWERSHELL%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-Work-PC.ps1" %* 2>>"%NCFTA_INSTALL_STARTUP_LOG%"
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" goto failed
if not exist "%NCFTA_INSTALL_STATUS_FILE%" (
    echo ERROR: PowerShell returned without confirming that setup completed.
    echo This is NOT a successful installation.
    set "RESULT=1"
    goto failed
)
echo.
echo Installer result:
type "%NCFTA_INSTALL_STATUS_FILE%"
goto finished
:failed
echo.
echo SETUP DID NOT COMPLETE. Exit code: %RESULT%
if exist "%NCFTA_INSTALL_STARTUP_LOG%" type "%NCFTA_INSTALL_STARTUP_LOG%"
echo Startup log: "%NCFTA_INSTALL_STARTUP_LOG%"
if exist "%NCFTA_INSTALL_LOG%" echo Detailed log: "%NCFTA_INSTALL_LOG%"
echo Keep this window open to read or photograph the error above.
:finished
echo.
echo Press any key to CLOSE this window.
pause >nul
exit /b %RESULT%
