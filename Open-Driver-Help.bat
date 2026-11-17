@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "GPU_HELP=https://docs.openvino.ai/2026/get-started/install-openvino/configurations/configurations-intel-gpu.html"
set "NPU_HELP=https://docs.openvino.ai/2026/get-started/install-openvino/configurations/configurations-intel-npu.html"
echo Intel GPU documentation: %GPU_HELP%
echo Intel NPU documentation: %NPU_HELP%
if /i "%~1"=="--list" exit /b 0
echo Use your work PC manufacturer's driver package or your IT software center first.
echo Opening Intel GPU and NPU setup documentation. No drivers are installed by this file.
set "RESULT=0"
start "" "%GPU_HELP%"
if errorlevel 1 set "RESULT=1"
start "" "%NPU_HELP%"
if errorlevel 1 set "RESULT=1"
if not "%RESULT%"=="0" echo Could not open the default browser. Copy the documentation links above into your browser.
echo.
echo Press any key to CLOSE this window.
pause >nul
exit /b %RESULT%
