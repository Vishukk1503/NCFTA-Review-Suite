@echo off
setlocal EnableExtensions DisableDelayedExpansion
title NCFTA Review Suite - BAT setup
echo NCFTA Review Suite - BAT setup v3.2
echo Folder: "%~dp0"
set "RESULT=1"
set "SETUP_PYTHON="
set "REQUESTED_PYTHON=%NCFTA_PYTHON%"
set "PLAN_ONLY="
set "OFFLINE_SETUP="
if not exist "%~dp0install_work_pc.py" (
    echo ERROR: install_work_pc.py is missing. Extract all files from the BAT update.
    goto finished
)
:parse_args
if "%~1"=="" goto find_python
if /i "%~1"=="--python" goto python_arg
if /i "%~1"=="-Python" goto python_arg
if /i "%~1"=="--plan" set "PLAN_ONLY=1"
if /i "%~1"=="-Plan" set "PLAN_ONLY=1"
if /i "%~1"=="--help" set "PLAN_ONLY=1"
if /i "%~1"=="-h" set "PLAN_ONLY=1"
if /i "%~1"=="--wheelhouse" set "OFFLINE_SETUP=1"
if /i "%~1"=="-Wheelhouse" set "OFFLINE_SETUP=1"
shift /1
goto parse_args
:python_arg
shift /1
if "%~1"=="" (
    echo ERROR: --python requires the full path to Python 3.12 x64.
    goto finished
)
set "REQUESTED_PYTHON=%~1"
shift /1
goto parse_args
:find_python
if defined REQUESTED_PYTHON (
    call :try_python "%REQUESTED_PYTHON%"
    if not defined SETUP_PYTHON (
        echo ERROR: The selected Python is missing or is not Python 3.12 x64.
        echo Check --python or NCFTA_PYTHON.
        goto finished
    )
    goto run_setup
)
call :discover_python
if defined SETUP_PYTHON goto run_setup
if defined PLAN_ONLY (
    echo Python 3.12 x64 is required. No installation was attempted in plan/help mode.
    goto finished
)
if defined OFFLINE_SETUP (
    echo ERROR: Offline setup requires Python 3.12 x64 already installed.
    goto finished
)
where winget.exe >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python 3.12 x64 and WinGet are unavailable.
    echo Install Python 3.12 x64 through your IT software center or python.org, then rerun this BAT.
    goto finished
)
echo Installing Python 3.12 through Windows Package Manager...
winget.exe install --id Python.Python.3.12 --exact --source winget --scope user --accept-package-agreements --accept-source-agreements
set "WINGET_RESULT=%ERRORLEVEL%"
call :discover_python
if not defined SETUP_PYTHON (
    echo ERROR: Python is still unavailable. WinGet exit code: %WINGET_RESULT%.
    echo Read the installation message above. IT may need to install Python 3.12 x64.
    goto finished
)
:run_setup
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
echo Python: "%SETUP_PYTHON%"
"%SETUP_PYTHON%" -X utf8 -u "%~dp0install_work_pc.py" %*
set "RESULT=%ERRORLEVEL%"
:finished
if not "%RESULT%"=="0" echo SETUP FAILED. Exit code: %RESULT%. Read the error above.
echo.
echo Press any key to CLOSE this window.
pause >nul
exit /b %RESULT%
:discover_python
call :try_python "%~dp0.venv\Scripts\python.exe"
if defined SETUP_PYTHON exit /b 0
call :try_python "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined SETUP_PYTHON exit /b 0
call :try_python "%ProgramFiles%\Python312\python.exe"
if defined SETUP_PYTHON exit /b 0
where py.exe >nul 2>&1
if not errorlevel 1 for /f "delims=" %%P in ('py.exe -3.12 -c "import sys; print(sys.executable)" 2^>nul') do call :try_python "%%P"
if defined SETUP_PYTHON exit /b 0
for /f "delims=" %%P in ('where python.exe 2^>nul') do call :try_python "%%P"
exit /b 0
:try_python
if defined SETUP_PYTHON exit /b 0
if not exist "%~1" exit /b 1
if /i "%~dp1"=="%LOCALAPPDATA%\Microsoft\WindowsApps\" exit /b 1
"%~1" -c "import sys,struct,platform; sys.exit(0 if sys.version_info[:2]==(3,12) and struct.calcsize('P')==8 and platform.machine().upper()=='AMD64' else 1)" >nul 2>&1
if errorlevel 1 exit /b 1
set "SETUP_PYTHON=%~1"
exit /b 0
