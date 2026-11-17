@echo off
setlocal EnableExtensions DisableDelayedExpansion
title NCFTA Review Suite
echo NCFTA Review Suite - starting...
set "RESULT=1"
if not exist "%~dp0Start-NCFTA.ps1" (
    echo ERROR: Start-NCFTA.ps1 is missing. Extract the complete suite first.
    goto finished
)
set "NCFTA_POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if defined PROCESSOR_ARCHITEW6432 set "NCFTA_POWERSHELL=%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%NCFTA_POWERSHELL%" (
    echo ERROR: Windows PowerShell was not found. Ask IT to check Windows PowerShell access.
    goto finished
)
"%NCFTA_POWERSHELL%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-NCFTA.ps1" %*
set "RESULT=%ERRORLEVEL%"
:finished
if not "%RESULT%"=="0" echo NCFTA did not complete successfully. Exit code: %RESULT%. Read the error above.
echo.
echo Press any key to CLOSE this window.
pause >nul
exit /b %RESULT%
