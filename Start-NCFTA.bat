@echo off
setlocal EnableExtensions DisableDelayedExpansion
title NCFTA Review Suite
echo NCFTA Review Suite - BAT launcher v3.2
set "RESULT=1"
set "SUITE_PYTHON="
if not exist "%~dp0suite.py" (
    echo ERROR: suite.py is missing. Extract the complete suite first.
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
if exist "%~dp0..\..\work\gpu-env\Scripts\python.exe" set "SUITE_PYTHON=%~dp0..\..\work\gpu-env\Scripts\python.exe"
if defined SUITE_PYTHON goto launch
echo ERROR: Python environment not found. Run Install-Work-PC.bat first.
goto finished
:launch
set "NO_COLOR="
set "TERM=xterm-256color"
set "COLORTERM=truecolor"
set "NCFTA_TERMINAL=1"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
pushd "%~dp0"
if errorlevel 1 (
    echo ERROR: Could not open the suite folder.
    goto finished
)
"%SUITE_PYTHON%" -X utf8 -u "%~dp0suite.py" %*
set "RESULT=%ERRORLEVEL%"
popd
:finished
if not "%RESULT%"=="0" echo NCFTA did not complete successfully. Exit code: %RESULT%. Read the error above.
echo.
echo Press any key to CLOSE this window.
pause >nul
exit /b %RESULT%
