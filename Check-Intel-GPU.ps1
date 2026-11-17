$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'Start-NCFTA.ps1') check-intel
Read-Host 'Press Enter to close'
