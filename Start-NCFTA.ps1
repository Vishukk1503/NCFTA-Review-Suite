$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonCandidates = @($env:NCFTA_PYTHON, (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'), (Join-Path $PSScriptRoot '..\..\work\gpu-env\Scripts\python.exe'))
$pythonPath = $pythonCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not $pythonPath) { Write-Host 'Run Install-Work-PC.bat first, or set NCFTA_PYTHON to an existing compatible environment.'; Read-Host 'Press Enter'; exit 1 }
# Set terminal capabilities only for this launcher and its child process.
Remove-Item Env:NO_COLOR -ErrorAction SilentlyContinue
$env:TERM = 'xterm-256color'
$env:COLORTERM = 'truecolor'
$env:NCFTA_TERMINAL = '1'
& $pythonPath -X utf8 (Join-Path $PSScriptRoot 'suite.py') @args
if (-not $args.Count) { Read-Host 'Press Enter to close' }
