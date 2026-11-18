@echo off
setlocal EnableExtensions DisableDelayedExpansion
title NCFTA Connection Check
set "RESULT=1"
set "SUITE_PYTHON="
if not exist "%~dp0check_connection.py" (
    echo ERROR: check_connection.py is missing. Extract both connection-check files beside Start-NCFTA.bat.
    goto finished
)
if not exist "%~dp0suite.py" (
    echo ERROR: Put both connection-check files in the existing suite folder beside Start-NCFTA.bat.
    goto finished
)
if defined NCFTA_PYTHON (
    if not exist "%NCFTA_PYTHON%" (
        echo ERROR: NCFTA_PYTHON points to a missing executable.
        goto finished
    )
    set "SUITE_PYTHON=%NCFTA_PYTHON%"
    goto launch
)
if exist "%~dp0.venv\Scripts\python.exe" set "SUITE_PYTHON=%~dp0.venv\Scripts\python.exe"
if defined SUITE_PYTHON goto launch
echo ERROR: Python environment not found. Use the installed suite folder.
goto finished
:launch
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
pushd "%~dp0"
if errorlevel 1 goto finished
"%SUITE_PYTHON%" -X utf8 -u "%~dp0check_connection.py"
set "RESULT=%ERRORLEVEL%"
popd
:finished
echo.
echo Press any key to CLOSE this window.
pause >nul
exit /b %RESULT%
