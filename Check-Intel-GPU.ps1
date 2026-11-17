$ErrorActionPreference = 'Stop'
try {
    & (Join-Path $PSScriptRoot 'Start-NCFTA.ps1') check-intel @args
    exit $LASTEXITCODE
} catch {
    Write-Host ('Intel check launch failed: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
