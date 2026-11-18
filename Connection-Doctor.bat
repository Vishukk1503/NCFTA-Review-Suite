@echo off
setlocal EnableExtensions DisableDelayedExpansion
title NCFTA Connection Doctor
set "RESULT=1"
set "DOCTOR_PYTHON="
if not exist "%~dp0connection_doctor.py" (
    echo ERROR: Extract all Connection Doctor files beside Start-NCFTA.bat.
    goto finished
)
if not exist "%~dp0connection_windows.py" (
    echo ERROR: connection_windows.py is missing. Extract the complete Connection Doctor ZIP.
    goto finished
)
if not exist "%~dp0connection_rca.py" (
    echo ERROR: connection_rca.py is missing. Extract all four files from the Connection Doctor ZIP.
    goto finished
)
if defined NCFTA_PYTHON (
    if not exist "%NCFTA_PYTHON%" (
        echo ERROR: NCFTA_PYTHON points to a missing executable.
        goto finished
    )
    set "DOCTOR_PYTHON=%NCFTA_PYTHON%"
    goto launch
)
if exist "%~dp0.venv\Scripts\python.exe" set "DOCTOR_PYTHON=%~dp0.venv\Scripts\python.exe"
if defined DOCTOR_PYTHON goto launch
echo ERROR: Put these files in the installed suite folder, beside Start-NCFTA.bat and .venv.
goto finished
:launch
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
pushd "%~dp0"
if errorlevel 1 goto finished
"%DOCTOR_PYTHON%" -X utf8 -u "%~dp0connection_doctor.py" %*
set "RESULT=%ERRORLEVEL%"
popd
:finished
echo.
echo Press any key to CLOSE this window.
pause >nul
exit /b %RESULT%
