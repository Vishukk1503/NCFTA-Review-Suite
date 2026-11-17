@echo off
echo NCFTA Intel GPU check
if not exist "%~dp0Start-NCFTA.bat" (
    echo ERROR: Start-NCFTA.bat is missing. Extract the complete suite first.
    echo Press any key to CLOSE this window.
    pause >nul
    exit /b 1
)
call "%~dp0Start-NCFTA.bat" check-intel %*
exit /b %ERRORLEVEL%
