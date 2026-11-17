@echo off
start "" powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-NCFTA.ps1" %*
